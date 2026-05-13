# Phase 3.5 Path A — Intraday-trigger fill mode (`NEXT_SESSION_OPEN` + `next_after_bar_close`)

> **Design document.** Locks the architecture and contract for closing
> the Phase 3.0 `xfail` acceptance test against QC's AAPL precomputed-
> predictions tutorial. Implementation plan follows separately
> (`writing-plans` skill).
>
> **Status:** Approved 2026-05-11 (brainstorming session with Tim).
> **Owner:** `feat/phase35-intraday-fill-mode` branch (to be created).
> **Predecessors:**
> - `docs/ml-predictions-authority.md` §10 "Path A"
> - `docs/references/reconciliations/qc-aapl-phase3.md` (Phase 3.0 xfail rationale)
> - `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md` (Phase 3 design)

---

## 1. Goal

Close the `xfail(strict=True)` mark on
`tests/research/parity/test_qc_aapl_phase3_trade_parity.py::test_qc_aapl_phase3_trade_level_parity`
by making our engine fill at the same physical bar QC fills at when both
consume the same precomputed-predictions input. The Phase 3.0
infrastructure (the reconciler, the fixture reader, the IBKR commission
model) stays unchanged; this design adds the missing engine + spec +
prediction-set primitives that make trade-by-trade parity achievable.

Success = the acceptance test removes its `xfail` mark and asserts
`report.status == "passed"` against a multi-day, minute-resolution QC
fixture covering 2026-02-10 → 2026-03-12.

Out of scope (deferred):

- A session-open *trigger* mode where the strategy handler fires on the
  first minute of each session rather than the prior day's consolidator
  close. That's "Path B" / option (b) — kept as a separate later
  proposal.
- Multi-symbol top-N ranking (Phase 4).
- `LoggedTrade` extension with explicit `qty`/`entry_fee`/`exit_fee` —
  used only if quantity drift surfaces as a Phase 3.5 gating failure.

## 2. Architecture overview

```
External capture
  └─ qc_orders.json (multi-day) + qc_price_history.csv (MINUTE, 2026-02-10 → 2026-03-12)

import_qc_fixture(qc_export.json)       # unchanged — keys at T 16:00 NY
  └─ PredictionSet artifact (manifest + chunks)

run_strategy_spec(...)
  ├─ data_source_factory yields MINUTE bars (FixtureDataReader auto-detects)
  ├─ PredictionSet.load(...)
  ├─ assert_bar_clock_coverage(set, bar_stream, refs=spec.predictions)   ← lookup-aware
  ├─ BacktestEngine.run(SpecAlgorithm)
  │   ├─ minute_bar loop
  │   ├─ 1440-min consolidator fires day-T daily bar on first minute of T+1
  │   ├─ SpecAlgorithm._on_consolidated_bar(day_T):
  │   │    └─ for each PredictionRef with lookup="next_after_bar_close":
  │   │         row = prediction_set.next_after(bar.end_time_ms)
  │   │         predictions[ref.id] = Decimal(str(row[ref.field]))
  │   │    └─ if entry triggers → ctx.set_holdings(...)
  │   └─ Engine order-drain (Step 5):
  │        FillMode.NEXT_SESSION_OPEN → attempt IMMEDIATE fill against current minute_bar
  │          - eligible (minute_bar.date() > signal_bar.end_time.date()): fills now
  │          - ineligible (same trading date): defers to pending_fills
  └─ returns RunLedger + BacktestRunResult

QcReconciler.reconcile_qc_aapl_phase3(...)
  └─ DECISION_MISMATCH, FILL_PRICE_DRIFT, PNL_DRIFT all expected to vanish
```

All new code lives in five existing files plus tests/fixtures. No new
engine event handlers, no new strategy callbacks, no calendar
dependency in the importer, no spec-resolution change (stays at 1440
min — daily-consolidator-over-minute-stream pattern).

### Two named primitives that change semantics

| Primitive | What it means | Where it surfaces |
|---|---|---|
| `PredictionRef.lookup` | **Data timing** — how the evaluator selects which prediction row to read at decision time. Values: `exact_bar_close` (current behavior, exact ts match) or `next_after_bar_close` (smallest row strictly greater than `bar.end_time_ms`). | Spec schema |
| `FillMode.NEXT_SESSION_OPEN` | **Execution timing** — fill on the first eligible minute bar after the signal bar's trading date. Independent of data timing; the spec must set both for QC-tutorial parity. | Engine fill model |

These two are intentionally orthogonal. A reader of a spec can see both
choices and reason about them independently: at day T's close, the
strategy intentionally consumes the next prediction row, and
intentionally fills at the next session's first eligible minute open.

## 3. Surface changes by file

### 3.1 `PythonDataService/app/engine/strategy/spec/schema.py`

Add a new lookup field on `PredictionRef`:

```python
PredictionLookup = Literal["exact_bar_close", "next_after_bar_close"]

class PredictionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    prediction_set_id: str
    field: str = "prediction"           # unchanged
    lookup: PredictionLookup = "exact_bar_close"   # new
```

Existing specs continue to validate (default preserves current
behavior). QC-style specs set `lookup="next_after_bar_close"`.

### 3.2 `PythonDataService/app/engine/execution/order.py`

Add the new `FillMode` enum value:

```python
class FillMode(Enum):
    SIGNAL_BAR_CLOSE = "signal_bar_close"
    NEXT_BAR_OPEN = "next_bar_open"
    NEXT_SESSION_OPEN = "next_session_open"   # new
```

### 3.3 `PythonDataService/app/engine/execution/fill_model.py`

Two additions:

1. Module-level `DEFERRED_FILL_MODES` set (single source of truth):

   ```python
   DEFERRED_FILL_MODES: frozenset[FillMode] = frozenset(
       {FillMode.NEXT_BAR_OPEN, FillMode.NEXT_SESSION_OPEN}
   )
   ```

2. `FillModel.fill_market_order` learns a `NEXT_SESSION_OPEN` branch:

   ```python
   elif self.mode == FillMode.NEXT_SESSION_OPEN:
       if next_bar is None:
           return None
       # Eligibility: candidate bar must belong to a trading date STRICTLY
       # AFTER the signal bar's trading date (NY-local). Minimal
       # implementation for regular-hours-only fixtures. A future
       # EligibilityPolicy would replace this date comparison without
       # changing the contract: "first eligible minute bar after the
       # signal bar's trading date."
       if next_bar.time.date() <= signal_bar.end_time.date():
           return None
       fill_price = next_bar.open
       fill_time = next_bar.time
   ```

`SIGNAL_BAR_CLOSE` and `NEXT_BAR_OPEN` branches unchanged. Slippage
application and `OrderEvent` construction stay shared across all modes.

**Tz-awareness assumption.** Both `signal_bar.end_time` and
`next_bar.time` carry `tzinfo=America/New_York` (set by both
`FixtureDataReader` and `LeanMinuteDataReader`). `.date()` on a tz-aware
datetime returns the NY-local calendar date. Tests assert `tzinfo is not
None and "New_York" in str(tzinfo)` so a naive-datetime regression is a
test failure rather than a wrong-date silent bug.

### 3.4 `PythonDataService/app/engine/engine.py`

Two changes to the main loop:

1. **Pending-fills loop dispatch (Step 3)** widens from a hard-coded
   `NEXT_BAR_OPEN` check to set membership in `DEFERRED_FILL_MODES`:

   ```python
   from app.engine.execution.fill_model import DEFERRED_FILL_MODES
   ...
   if pending_fills and self.fill_model.mode in DEFERRED_FILL_MODES:
       ...
   ```

2. **Order-drain branch (Step 5)** gains a new `NEXT_SESSION_OPEN`
   case that **attempts immediate fill against the current `minute_bar`
   before deferring**. This is the critical correction that produces
   trade-by-trade QC parity:

   ```python
   for order in market_orders:
       if self.fill_model.mode == FillMode.SIGNAL_BAR_CLOSE:
           event = self.fill_model.fill_market_order(order, signal_bar, next_bar=None)
           # ... apply, log, register bracket
       elif self.fill_model.mode == FillMode.NEXT_SESSION_OPEN:
           # Attempt immediate fill — the current minute_bar may already
           # be the first minute of the next session (consolidator-fire
           # on rollover with daily resolution over minute data).
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
           # Existing behavior — always defer; protects single-stream
           # fixtures where signal_bar IS the current minute_bar.
           pending_fills.append((order, signal_bar))
       else:
           raise ValueError(f"unknown fill mode: {self.fill_model.mode}")
   ```

   The asymmetry between `NEXT_BAR_OPEN` (always defer) and
   `NEXT_SESSION_OPEN` (opportunistic immediate) is deliberate:
   `NEXT_BAR_OPEN`'s `signal_bar` and `minute_bar` are typically the
   same physical bar (resolution=1, no consolidation), so filling
   against `minute_bar` would defeat the "next" semantic.
   `NEXT_SESSION_OPEN` is designed for the consolidator pattern where
   `signal_bar` (the daily consolidated bar, end_time T 16:00 NY) and
   `minute_bar` (the actual stream bar, e.g. T+1 09:30 NY) are distinct
   — so filling against `minute_bar` *is* the "next session" semantic
   when the trading date has rolled.

A one-line comment at the top of the main loop points at
`test_engine_fill_modes.py::test_next_session_open_fills_at_first_eligible_minute`
as the proof of the step-ordering invariant.

### 3.5 `PythonDataService/app/research/runs/runner.py`

`_VALID_FILL_MODES` gains `"next_session_open"`; `_parse_fill_mode`
gains a third branch. `_normalize_fill_mode` is unchanged (it's
mode-agnostic). Normalization happens at runner entry (line 282), before
any ledger identity, `spec_hash`, or `data_snapshot_id` capture — so
`"NEXT-SESSION-OPEN"`, `"next_session_open"`, `"Next_Session_Open"`,
etc. all produce the same canonical ledger and the same
`data_snapshot_id`.

### 3.6 `PythonDataService/app/research/ml/loader.py`

`PredictionSet` gains a sorted-key index plus the new lookup:

```python
@dataclass
class PredictionSet:
    manifest: PredictionSetManifest
    index: dict[int, dict[str, float]]
    _sorted_ts: list[int] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._sorted_ts = sorted(self.index.keys())

    def next_after(self, ts_ms: int) -> dict[str, float] | None:
        """Smallest-key row whose timestamp is strictly greater than ``ts_ms``.

        Returns ``None`` when no such row exists. Callers needing
        strict-non-None guarantees (the evaluator) raise
        ``PredictionLookupError`` on None; the coverage check (§3.7) is
        the intended first-line guard.
        """
        i = bisect_right(self._sorted_ts, ts_ms)
        if i == len(self._sorted_ts):
            return None
        return self.index[self._sorted_ts[i]]
```

A new `PredictionLookupError` exception (defined alongside the existing
`PredictionCoverageError` in `loader.py`) is raised by the evaluator on
runtime contract violations:

```python
class PredictionLookupError(ValueError):
    """Raised when a strategy's per-bar prediction lookup violates contract.

    Indicates one of: missing 'next' row for a next_after_bar_close ref,
    a missing exact-match row, or a row missing the declared field.
    Coverage check is the intended first-line guard; this is the
    runtime backstop so a coverage bug or fixture truncation can never
    silently suppress trades.
    """
```

### 3.7 `PythonDataService/app/research/ml/coverage.py`

`assert_bar_clock_coverage` becomes lookup-aware. Caller passes the
spec's actual `PredictionRef` list; the function validates the
(lookup, field) pair on every fired bar:

```python
def assert_bar_clock_coverage(
    prediction_set: PredictionSet,
    bar_stream: Iterable[int],            # iterable of bar.end_time_ms — fired-bar timestamps
    *,
    refs: Iterable[PredictionRef],
) -> None:
    """Validate every fired bar has a matching prediction for EACH declared ref.

    For each ``ref`` and each fired ``ts_ms`` in ``bar_stream``:

    - ``ref.lookup == "exact_bar_close"``: ``prediction_set.index`` must
      contain ``ts_ms``, AND the row must contain ``ref.field``.
    - ``ref.lookup == "next_after_bar_close"``: there must be a row with
      timestamp strictly greater than ``ts_ms``, AND that row must
      contain ``ref.field``.

    Raises ``PredictionCoverageError`` on the first violation. The
    message reports: ref.id, ref.lookup, fired ts_ms, ref.field;
    additionally the matched next-row ts_ms when next_after_bar_close
    found a row but the field was missing.
    """
```

#### "Fired bar" definition

A consolidated bar that the engine *will* emit to the strategy handler
— i.e., every consolidated bar whose period has fully elapsed within
the data window. For daily resolution over a minute stream, this is
every trading day from the first through the **second-to-last**; the
trailing day's working bar never fires because no successor minute
arrives. `iter_consolidated_bar_end_times` (a thin projection of the
existing `iter_consolidated_bars`) mirrors that set exactly, so
coverage and runtime see the identical bar set.

#### Trailing-bar policy

No special-casing. Every fired bar must satisfy every active lookup
mode. The runner naturally trims the trailing non-executable signal
(consolidator never fires it); the coverage check sees only fired bars
and therefore never has to "skip" a final bar.

#### Error messages

```
# exact_bar_close, no row at fired ts:
"ref qc_pred (exact_bar_close): no prediction row at fired bar ts_ms=… (2026-02-10 16:00 NY); field='prediction'"

# exact_bar_close, row present but field missing:
"ref qc_pred (exact_bar_close): row at fired bar ts_ms=… is missing field 'confidence' (available: ['prediction'])"

# next_after_bar_close, no later row:
"ref qc_pred (next_after_bar_close): fired bar ts_ms=… has no prediction row strictly after; field='prediction'"

# next_after_bar_close, later row present but field missing:
"ref qc_pred (next_after_bar_close): fired bar ts_ms=… matched next row at ts_ms=… but it is missing field 'confidence' (available: ['prediction'])"
```

### 3.8 `PythonDataService/app/engine/strategy/spec/evaluator.py`

The `predictions` block in `_on_consolidated_bar` becomes per-ref
dispatch with a runtime backstop:

```python
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

Two design points:

- **Per-ref branch.** A spec with two refs may mix lookup modes; the
  coverage check validates each independently. No global "evaluator
  mode."
- **Strict runtime errors.** `None` returns from `next_after` or missing
  fields raise `PredictionLookupError`, never silently produce a False
  `PredictionComparison`. The coverage check is the intended first
  line; this is the runtime backstop so a fixture truncation, a
  coverage bug, or a code change to the bar set can't quietly suppress
  trades.

## 4. Fill-timing invariant (the critical guarantee)

Tracing the engine main loop on the consolidator-fire iteration (= first
minute of T+1, the bar `[09:30, 09:31)` in NY-local):

1. Iteration enters with `minute_bar = [09:30, 09:31)`.
2. **Step 3 — pending-fills loop:** `pending_fills` is empty (no order
   from day T yet — consolidator hasn't fired).
3. **Step 4 — consolidator:** day-T daily bar fires.
   `_on_consolidated_bar` looks up `next_after(T 16:00 NY)`, gets
   prediction for T+1, submits market order.
4. **Step 5 — order drain:** `NEXT_SESSION_OPEN` attempts immediate fill
   against current `minute_bar = [09:30, 09:31)`. Eligibility:
   `[09:30, 09:31).date() == T+1 > T = signal_bar.end_time.date()`.
   **ELIGIBLE.** Fill at `next_bar.open = open of [09:30, 09:31)`.
   `event.fill_time = next_bar.time = T+1 09:30 NY`.

**Pinned invariant:**

- `event.fill_time == T+1 09:30 NY` (start of bar `[09:30, 09:31)`)
- `event.fill_price == open_of_bar[09:30, 09:31)`
- `pending_fills` is empty at the end of this iteration's Step 5 (the
  off-by-one guard — anything left in `pending_fills` indicates a
  step-ordering regression that would shift the fill one bar later).

QC's reported fill at "09:31 ET" labels the same physical bar by its
*end-time* (their convention). The reconciler aligns on
`(trading_date, side)` and compares fill *price*; the labeling
convention difference between our `bar.time` (start) and QC's bar
end-time labels has no effect on alignment because the reconciler's
minute-audit branch confirms the price falls inside the bar's
`[low, high]` range.

The synthetic engine test (`test_engine_fill_modes.py`) asserts the
above invariant exactly and stays as the primary protection against
any future engine refactor that might shift the step ordering.

## 5. Fixture re-capture

The current Phase 3.0 fixture (`tests/fixtures/golden/qc-aapl-phase3/`)
is daily and single-day; it is replaced **in place** by a multi-day
minute capture. Git history is the audit trail for the previous shape.
The reconciliation doc (`docs/references/reconciliations/qc-aapl-phase3.md`)
gets a "Replaced 2026-05-11" note.

| Parameter | Value |
|---|---|
| Algorithm | unchanged: AAPL-only, `set_holdings(AAPL, 1 if pred > 0 else 0)` from the existing runbook |
| Window | 2026-02-10 → 2026-03-12 (matches PR #215's prediction-set window) |
| Resolution | `Resolution.MINUTE` |
| Fee branch | A (`orderFeeAmount` non-zero, smoke test re-pins) |

Smoke-test assertions added (caught from §6 R10):

- `min(timestamps) == datetime(2026, 2, 10, 9, 30, tzinfo=NY)`
- `max(timestamps) == datetime(2026, 3, 12, 15, 59, tzinfo=NY)` (or whatever
  the actual capture lands on — pinned at capture time per the runbook)
- `is_minute_resolution == True`
- `tzinfo is not None and "New_York" in str(tzinfo)` on every parsed bar

The pinned `prediction_set_hash`
(`b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e`)
is unaffected — the predictions themselves don't change. PR #215's
fixture and its hash stay valid.

The orphan `qc_appl_bars.csv` at repo root is confirmed stale (1,170
rows, 2026-02-09 → 2026-02-11 window — different from the intended
fixture window) and is deleted as part of the PR's housekeeping
commit.

## 6. Test plan

### Files and cases

| Test file | New cases |
|---|---|
| `test_fill_model.py` (extend) | `NEXT_SESSION_OPEN_defers_when_candidate_same_date`; `NEXT_SESSION_OPEN_fills_when_candidate_later_date`; `NEXT_SESSION_OPEN_applies_slippage_correctly`; `DEFERRED_FILL_MODES_membership_invariant` |
| `test_engine_fill_modes.py` (new) | Exact-timestamp invariant (§4): fill at `T+1 09:30 NY`, `pending_fills` empty after Step 5; multi-T+1-minute case (R9); same-trading-date signal stays pending across T-minutes then fills at first T+1 open; consolidator-rollover-fires-and-fills-immediately |
| `test_prediction_set.py` (new or extend `test_loader.py`) | `next_after_returns_smallest_strictly_greater`; `next_after_returns_None_on_end_of_window`; `next_after_handles_unsorted_input_chunks` |
| `test_coverage.py` (extend) | `exact_bar_close_missing_row_raises`; `exact_bar_close_missing_field_raises`; `next_after_bar_close_no_later_row_raises`; `next_after_bar_close_later_row_missing_field_raises_with_matched_ts_in_message`; `mixed_lookup_modes_both_validated`; `single_ref_with_trailing_gap_in_coverage_caught` |
| `test_evaluator.py` (extend) | `next_after_bar_close_consumes_next_row_at_decision_time`; `PredictionLookupError_when_coverage_bypass_attempts_runtime_lookup_failure` |
| `test_qc_fixture_smoke.py` (extend) | Minute resolution detected; first/last timestamp pinning (§5); tz-awareness; `FEE_PRESENCE_BRANCH=A` |
| `test_qc_aapl_phase3_trade_parity.py` (the acceptance test) | Remove `@pytest.mark.xfail`. Update `_aapl_spec()` to set `lookup="next_after_bar_close"`. Update `RunRequest` to `fill_mode="next_session_open"`. Assert `report.status == "passed"`. Assert three pinned exact-aligned trade rows: first fill, mid-window position turnover, last fill — each `(trading_date, side, qty, fill_price, fee, fill_time_ms)` matches QC's recorded fill. Render success report to `artifacts/reconciliations/qc-aapl-phase3-latest.md` on both pass and fail (default-on for this test). |
| `test_runner_inmemory.py` (extend) | `fill_mode_normalization_round_trips_for_next_session_open`; `same_run_with_dash_vs_underscore_vs_caps_produces_identical_ledger_hash_and_data_snapshot_id` |

### Pre-push hygiene

Project-scope checks before pushing:

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
podman exec polygon-data-service python -m pytest /app/tests -q \
  --ignore=/app/tests/integration \
  --ignore=/app/tests/fixtures/test_golden_manifest.py \
  -k "not slow"
```

Pre-existing baselines surfaced in the PR description: the IBKR
client-id default test failure (env override) and the `jsonschema`
collection error remain. This PR adds zero new pre-existing failures.

## 7. Risks

### 7.1 DST transition mid-window

The fixture window spans the 2026-03-08 DST spring-forward (US Eastern).
Traced through the codebase:

- `TradeBarConsolidator._floor_to_period` uses tz-aware subtraction, so
  the 86400-second floor stays correct across the seam.
- `FixtureDataReader` parses naive timestamps and then `.replace(tzinfo=NY)`,
  which does **not** disambiguate the 02:00 AM DST seam. Regular-session
  bars (09:30–16:00) are nowhere near that seam, so the path is safe —
  but a code comment is added at the parser site, and the smoke test
  asserts `tzinfo is not None` on every parsed bar.

Low risk; mitigation is comment + smoke-test guard rather than trusting
"no one will run at 02:30 AM."

### 7.2 Engine step-ordering invariant

Path A relies on the fixed order in `engine.run()` main loop: (3)
pending-fills → (4) consolidator-fire → (5) order-drain. A future engine
refactor that re-orders these — even harmlessly-looking for non-
prediction strategies — could shift the fill by a bar. The exact-
timestamp assertion in `test_engine_fill_modes.py` is the regression
guard; a one-line comment at the top of the main loop in `engine.py`
points there.

### 7.3 QC capture non-determinism

Re-running the QC backtest in QC Cloud can produce micro-differences
(order IDs, capture wall-clock, occasionally subprice differences if
QC's minute pipeline re-runs). The fixture is the ground truth; we
don't re-run QC at test time. The smoke test diffs against pinned
(first ts, last ts, row count) tuples; the acceptance test diffs
against three pinned trade rows. Both surface regressions loudly if a
future re-capture lands.

### 7.4 Quantity drift (existing known gap)

`LoggedTrade` has no `qty` field; `_build_our_fills` reconstructs
shares from `floor(running_equity / entry_price)`. QC may apply a small
cash-buffer factor (1-2 shares) producing `QUANTITY_MISMATCH`
divergences. If these surface as gating failures:

- **(a)** Match QC's `set_holdings` cash-buffer convention in our
  sizing — preferred, keeps `LoggedTrade` minimal.
- **(b)** Widen `QUANTITY_MISMATCH`'s atol with explicit reasoning in
  the reconciliation report — fallback only if (a) isn't analytically
  reproducible.
- **(c)** Extend `LoggedTrade` with `qty`/`entry_fee`/`exit_fee` — last
  resort.

Plan is (a); not committing to (c) up front because the design is
deliberately conservative about schema growth.

### 7.5 In-place fixture replacement

The previous daily/single-day fixture is replaced in place. Reviewers
should:

- Read the "Artifact replacement" bullet in the PR description.
- Pull the branch and run the smoke test rather than diff the CSV
  (~8000 rows vs 4).
- Reference git history (`git log -- tests/fixtures/golden/qc-aapl-phase3/`)
  for the prior shape if needed.

## 8. Acceptance criteria

1. `test_qc_aapl_phase3_trade_level_parity` removes `@pytest.mark.xfail`
   and asserts `report.status == "passed"` against the multi-day
   minute fixture.
2. The three pinned aligned-fill rows match QC exactly under the
   default tolerances from `Tolerances.phase3_default()` — no
   tolerance loosening to pass the test.
3. `test_engine_fill_modes.py` asserts `event.fill_time == T+1 09:30 NY`
   and `event.fill_price == open_of_bar[09:30, 09:31)` for the
   synthetic Path A scenario, with `pending_fills` empty after Step 5.
4. `test_coverage.py` exercises all four error-message shapes from §3.7.
5. Project-scope `ruff check` produces zero warnings.
6. Project-scope pytest run shows zero new failures relative to the
   pre-existing baseline.
7. `docs/ml-predictions-authority.md` §7 Phase 3.5 row flips from
   "⏳ pending" to "✅ shipped"; §10 "Phase 3.5" subsection is
   collapsed to a one-line historical note; "Last reviewed" date
   bumps.
8. `docs/references/reconciliations/qc-aapl-phase3.md` is rewritten
   to describe the passing reconciliation rather than the documented
   xfail rationale.

## 9. Out of scope

Reserved for follow-up work, explicitly **not** in this PR:

- **Path B** — session-open trigger event handler. (Same QC parity,
  different architecture; deferred to its own brainstorming.)
- **Phase 4** — multi-symbol top-N ranking. Independent of Path A;
  needs `PortfolioConstruction` schema extension, multi-symbol bar
  streaming, multi-symbol `PredictionSet`.
- **`LoggedTrade` schema extension** with `qty`/fees — only if §7.4
  forces it.
- **Pre/post-market eligibility policy** — the `NEXT_SESSION_OPEN`
  contract is worded to accommodate this without rewording, but the
  policy itself isn't built.
- **Wiring `IbkrEquityCommissionModel` into the engine** — stays
  reconciler-side per `ml-predictions-authority.md` §10 "Don't add
  without a real reason."
