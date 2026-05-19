# LEAN ↔ Engine parity on Polygon-sourced bars

**Status:** Design approved 2026-05-19. Awaiting implementation plan (writing-plans).
**Authors:** Tim (owner), Claude (design).
**Branch:** `feat/backfill-lean-runs` (current).
**Related docs:** `.claude/rules/numerical-rigor.md`, `docs/superpowers/specs/2026-05-19-lean-ema-template-and-unified-history-design.md`, `.claude/skills/reconcile-backtest/SKILL.md`.

## Problem

The LEAN EMA-crossover template shipped in PR #291 cannot prove parity with the Engine Lab implementation, because the validation plane is ungrounded:

1. **`run_trusted_sample()` does not consume Polygon data.** It stages synthetic deci-cent minute bars (`lean_sidecar_service.py:249`). Any LEAN-vs-engine assertion built on this proves only toy-data behavior — it does not prove that the production data path produces equal indicator state, equal decisions, and equal fills.
2. **The requested symbol is not passed into the LEAN template.** `EMA_CROSSOVER_SOURCE` reads `GetParameter("symbol")` and falls back to `"SPY"` (`ema_crossover.py:40`). The orchestrator only writes `start_date`, `end_date`, `starting_cash` to `LeanConfig.parameters` (`lean_sidecar_service.py:466`). For non-SPY symbols, the sidecar can stage one symbol while the algorithm subscribes to another.
3. **Timeframe and session handling are fragmented.** `polygon_export.py` keeps extended-hours bars unless filtered upstream; `dataset_service.py` defaults session to `extended`; the EMA template uses `Raw` normalization while `export_polygon_range_to_lean` defaults `adjusted=True`. There is no single contract saying whether the strategy's 15-minute bars come from native Polygon 15m aggregates or from 1m consolidated by each engine.
4. **No bar-level state receipt exists from LEAN.** The template computes EMA/RSI/cross/hold state in memory; it does not emit a per-consolidated-bar trace comparable to the engine's `DecisionSnapshot` (`spy_ema_crossover.py:246-253`). Reconciliation can therefore only happen at the trade level, which conflates indicator-math drift with decision-logic drift.
5. **The existing parity test does not run LEAN.** `test_spec_spy_ema_parity.py` compares `SpecAlgorithm` against the hand-coded `SpyEmaCrossoverAlgorithm` on synthetic bars. The LEAN container is never invoked.

This branch must not merge a "validation" feature without at least one pinned-Polygon-window LEAN-vs-engine receipt proving the two engines ingest the same bars and produce the same per-bar state and trades.

## Goals

- **G1.** A canonical Polygon 1-minute bar source for the sidecar, with RTH/extended as an explicit declared knob and `Raw` adjustment policy pinned.
- **G2.** Symbol, bar resolution, session, and adjustment plumbed into the LEAN template via `GetParameter` so the algorithm subscribes to exactly what was staged.
- **G3.** A per-consolidated-bar state CSV emitted from the LEAN EMA template, structurally comparable to the engine's `DecisionSnapshot`.
- **G4.** One narrow integration test that loads a pinned recorded Polygon fixture, runs LEAN and the engine against the same bars, and asserts (a) per-bar state equivalence within `atol=1e-9` on EMA/RSI and (b) trade-by-trade equivalence within `$0.01` fill-price tolerance.
- **G5.** A run manifest extension recording the data-provenance and bar-construction policy a reviewer can grep without re-reading code.

## Non-goals (this branch)

- Unified backtest-run persistence for engine-source runs (`engine_persistence.py`, `spec_strategy_runner.py`). The untracked WIP files conflict with `BacktestRunPersistenceService.cs:35-40` (`source != "lean-sidecar"` rejected). Either fix the backend contract or leave the files alone — out of scope here. This branch ships sidecar data parity; the engine-side persistence is a follow-up branch.
- Polygon-native 15m aggregate ingestion as a strategy bar source. Phase-1 parity is intraday-1m-canonical-only. Daily and higher resolutions are explicitly supported via Polygon-native `day` bars (existing `export_polygon_daily_bars_to_lean` path) — for a daily strategy, `data_policy.input_bars` and `data_policy.strategy_bars` would both be `{timespan: "day", multiplier: 1}` — but no new code is added for that path in this branch since the EMA template is intraday-only.
- The reconciler taxonomy plumbing into the test assertion. The narrow test uses direct `atol`/`rtol` comparisons; future tests that need per-divergence classification will route through `qc_reconciler.DivergenceCategory`.
- Tunable EMA-crossover parameters in the LEAN template. Period/gap/RSI band remain pinned to the spec.
- UI changes. This branch is pure backend.
- Backfill or migration of pre-existing synthetic-bar runs.

## Approach summary

| Decision | Choice |
|---|---|
| Coexistence with synthetic path | **Single orchestrator, branching at staging.** `TrustedRunRequest` gains `data_source: Literal["synthetic", "polygon"] = "synthetic"`. Buy-and-hold and reconciliation templates keep working unchanged. |
| Polygon data source | **Polygon 1-minute, raw, RTH by default.** Both engines consolidate to the strategy timeframe internally. Native daily for any future daily-resolution strategy. |
| Session policy | **`regular` default; `extended` is a declared knob in the run request and the manifest.** Filter is applied at the canonical-bars stage so both engines receive identical input zips. |
| Test data | **Recorded fixture + freshness canary.** Fixture under `tests/fixtures/polygon_capture/`. Canary at `tests/slow/`, gated on `POLYGON_API_KEY`. |
| Data-provider abstraction | **Protocol seam, not a test override.** `PolygonProvider` for production; `RecordedPolygonFixtureProvider` for tests. No `bars_override` parameter on the public fetch function. |
| State-trace mechanism | **Test-local, minimal.** LEAN template writes `state.csv`; parity test wraps engine strategy's bar handler with a recording closure that appends `last_decision_snapshot` to a list. No production engine changes. |
| Manifest | **New `data_policy` sub-block** with explicit input-bars / strategy-bars / session / adjustment / fixture identity. |

## Architecture

```
Polygon REST  ─PolygonProvider─▶  fetch_canonical_minute_bars
                                          │  (RTH filter, monotonicity check, dedup-reject)
                                          ▼
                          list[(date, list[TradeBar])]
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  │                                               │
          stage_minute_bars                              InMemoryDataReader
          stage_quote_bars                               (existing engine path)
          stage_daily_bars (aggregated)                          │
                  │                                               │
                  ▼                                               ▼
        LEAN container                                BacktestEngine
        EMA template:                                 SpyEmaCrossoverAlgorithm:
          consolidates 1m → 15m                         consolidates 1m → 15m
          emits state.csv (post-warmup)                 sets last_decision_snapshot
          emits order events                            emits OrderEvent stream
                  │                                               │
                  └─────────────────► parity test ◄───────────────┘
                                  asserts: state CSV ≡ snapshots
                                           order events ≡ engine trades
```

**Invariant:** both engines see byte-identical 1-minute trade and quote zips, derived from the same Polygon fixture. Any divergence is logic, not data.

### Provider seam

```python
# app/lean_sidecar/polygon_canonical.py

class CanonicalBarsProvider(Protocol):
    def fetch_minute_bars(
        self, *, symbol: str, start_date: date, end_date: date, adjusted: bool
    ) -> list[dict[str, Any]]:
        """Return Polygon-style bar dicts: timestamp (ms UTC, start-of-bar), open, high, low, close, volume."""

class PolygonProvider:
    def __init__(self, polygon: PolygonClientService): ...
    def fetch_minute_bars(self, *, symbol, start_date, end_date, adjusted) -> list[dict]:
        # delegates to fetch_bars_chunked(timespan="minute", multiplier=1, adjusted=adjusted)

class RecordedPolygonFixtureProvider:
    def __init__(self, fixture_path: Path): ...
    def fetch_minute_bars(self, *, symbol, start_date, end_date, adjusted) -> list[dict]:
        # loads bars.json; asserts (symbol, range, adjusted) match attribution.md; returns dicts
```

`fetch_canonical_minute_bars(*, symbol, start_date, end_date, session, adjustment, provider) -> list[tuple[date, list[TradeBar]]]` accepts the provider; production callers construct `PolygonProvider`; tests construct `RecordedPolygonFixtureProvider`. No conditional or override parameter on the public fetch function.

## Components

### New files

- **`PythonDataService/app/lean_sidecar/polygon_canonical.py`**
  - `CanonicalBarsProvider` protocol.
  - `PolygonProvider`, `RecordedPolygonFixtureProvider` concrete implementations.
  - `fetch_canonical_minute_bars(...)`: orchestrates fetch → session filter → monotonicity + duplicate rejection (fail-fast per `.claude/rules/numerical-rigor.md` § "External-API ingestion") → group by ET trading date → convert to `TradeBar` via the existing `polygon_export._polygon_bar_to_trade_bar` helper (reused, not duplicated).
- **`PythonDataService/tests/fixtures/polygon_capture/<window-id>/bars.json`** — captured Polygon minute bars for the parity window. `<window-id>` follows `<symbol>_minute_<from>_<to>` (e.g., `spy_minute_2025-01-06_2025-01-10` if that proves to be the chosen window — see "Fixture selection" below).
- **`PythonDataService/tests/fixtures/polygon_capture/<window-id>/attribution.md`** — captures: source URL/SDK version, fetch timestamp, parameters (adjusted, session pre-filter applied: none — raw Polygon response), bar count, **observed trade count when run through the LEAN EMA template** (gate for "is this window meaningful?"), `bars.json` SHA-256.
- **`PythonDataService/scripts/regenerate_polygon_fixture.py`** — operator script: fetches from live Polygon, writes `bars.json` + updates `attribution.md`. Commit messages explain regenerations per fixture-lifecycle rule.
- **`PythonDataService/tests/integration/test_lean_engine_polygon_parity.py`** — the receipt test. Single test function. Loads the fixture, runs LEAN sidecar with `data_source="polygon"`, runs the engine with `InMemoryDataReader`, asserts state-CSV parity + trade parity. Skipped with clear `pytest.skip(reason=...)` if `LEAN_LAUNCHER_URL` is unset.
- **`PythonDataService/tests/slow/test_polygon_fixture_freshness.py`** — `@pytest.mark.slow`. Skipped without `POLYGON_API_KEY`. Re-fetches the recorded window via `PolygonProvider` and asserts byte-equivalence on the recorded fields against `bars.json`.
- **`PythonDataService/tests/lean_sidecar/test_polygon_canonical.py`** — unit tests for `fetch_canonical_minute_bars`: RTH filter correctness (boundary minutes at 09:30:00 inclusive and 16:00:00 exclusive in NY local; pre-market and after-hours bars dropped when `session="regular"`), monotonicity rejection (assert raises on non-strictly-increasing timestamps), duplicate rejection (assert raises on duplicate timestamps). Uses synthetic dicts via a stub provider; no Polygon network.

### Touched files

- **`PythonDataService/app/services/lean_sidecar_service.py`**
  - `TrustedRunRequest`: add fields
    - `data_source: Literal["synthetic", "polygon"] = "synthetic"`
    - `bar_minutes: int = 15`
    - `session: Literal["regular", "extended"] = "regular"`
    - `adjustment: Literal["raw"] = "raw"` (only `raw` supported in Phase 1; `Literal` keeps the door open without expanding it)
  - `run_trusted_sample`: branch on `request.data_source`. Synthetic path unchanged. Polygon path:
    - Obtain provider via `polygon_canonical.get_default_provider()` — the factory tests monkey-patch to inject `RecordedPolygonFixtureProvider`. Production-side, the factory returns `PolygonProvider(get_polygon_client())`. No provider parameter on `run_trusted_sample` itself; this keeps the FastAPI router's call site shape stable.
    - Call `fetch_canonical_minute_bars(..., provider=provider)` → `bars_by_date`
    - Feed into existing `stage_minute_bars` / `stage_quote_bars` chain
    - Daily aggregation continues via existing `_aggregate_daily_bar`
  - `LeanConfig.parameters`: add `symbol`, `bar_minutes`, `session`, `adjustment` alongside existing `start_date`, `end_date`, `starting_cash`.
- **`PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`**
  - Read `bar_minutes` (default `15`), `session` (default `"regular"`), `adjustment` (default `"raw"`) from `GetParameter`.
  - `AddEquity(..., Resolution.Minute, fillForward=False, extendedMarketHours=(session == "extended"))`.
  - `SetDataNormalizationMode` chosen by `adjustment` (Phase 1 only `raw` → `DataNormalizationMode.Raw`; unknown values raise to fail loud).
  - Use `bar_minutes` for the `TradeBarConsolidator`.
  - After warmup is complete (post the `IsReady` check), append one row to `<ObjectStore>/state.csv` per consolidated bar with columns `ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,position_qty,signal`.
    - `ts_ms_utc`: `bar.EndTime` → int64 ms UTC (using `Time.DateTimeToUnixTimeStamp` or equivalent; the conversion lives inside the template to keep the manifest's "single boundary" rule).
    - `close`: float of `bar.Close`.
    - `cross_state`: one of `below`, `equal`, `above` based on `fast` vs `slow` at the current bar (not the previous-bar latch — that's `prev_*`).
    - `position_qty`: `int(self.Portfolio[self.symbol].Quantity)`.
    - `signal`: one of `HOLD`, `ENTER`, `EXIT` — matches the engine's `DecisionSnapshot.signal` enum exactly.
- **`PythonDataService/app/lean_sidecar/manifest.py`**
  - New `DataPolicyManifest` dataclass with fields and shape:
    ```python
    @dataclass(frozen=True, slots=True)
    class BarsSpec:
        timespan: Literal["minute", "hour", "day"]
        multiplier: int

    @dataclass(frozen=True, slots=True)
    class DataPolicyManifest:
        source: Literal["synthetic", "polygon"]
        symbol: str
        adjusted: bool                # False for "raw"; mirrors LEAN's Boolean for clarity
        session: Literal["regular", "extended"]
        input_bars: BarsSpec          # what we fetched
        strategy_bars: BarsSpec       # what the algorithm consumes
        timestamp_policy: Literal["bar_close_ms_utc"]
        timezone: Literal["America/New_York"]
        fixture_id: str | None        # set when source="polygon" and fed by RecordedPolygonFixtureProvider
        fixture_sha256: str | None    # SHA-256 of bars.json; set when source="polygon" via fixture; None for live
    ```
  - `RunManifest` gains `data_policy: DataPolicyManifest`. Schema version bumps.
  - `_build_manifest` in `lean_sidecar_service.py` populates it. For `synthetic` runs: `source="synthetic"`, `symbol=request.symbol`, `adjusted=False`, `session="regular"`, `input_bars=BarsSpec("minute", 1)`, `strategy_bars=BarsSpec("minute", request.bar_minutes)`, `fixture_id=None`, `fixture_sha256=None`. For `polygon` runs: populated from the request + provider.
  - Existing top-level `fill_forward`, `data_adjustment_policy`, `data_normalization_mode` fields stay where they are — the `data_policy` block is additive provenance, not a replacement for execution-policy fields. Reviewers grep the block for "what bars did this run consume and where did they come from"; the existing fields answer "what execution rules applied."
  - **Vocabulary note:** `data_policy.adjusted` is the upstream Polygon request flag (`adjusted=False` means raw prices, no split/dividend adjustment). `data_normalization_mode = "Raw"` is the LEAN-side enum encoding the same intent in LEAN's vocabulary. The two must agree (`adjusted=False ⇔ "Raw"`); a future writing-plans task enforces this with a runtime assertion at manifest construction.

### Explicitly NOT touched

- `engine_persistence.py`, `spec_strategy_runner.py` — WIP, owner unconfirmed, conflict with backend contract.
- `BacktestRunPersistenceService.cs` — out of scope.
- `polygon_export.py` — already supports `adjusted=False` via the resolution-routing wrapper; no change.
- `polygon_ingest.py`, `dataset_service.py`'s session defaults — orthogonal.
- The Engine Lab spec at `spy_ema_crossover.spec.json` — Phase 1's third oracle works as-is.
- `SpyEmaCrossoverAlgorithm` — already produces the per-bar snapshot; no changes.

## Data flow (parity test, end-to-end)

1. **Load fixture.** `RecordedPolygonFixtureProvider(Path(".../<window-id>"))` loads `bars.json` and `attribution.md`. Provider's `fetch_minute_bars(...)` returns dicts identical in shape to a live Polygon response.
2. **Build canonical bars once.** `fetch_canonical_minute_bars(symbol="SPY", start_date=..., end_date=..., session="regular", adjustment="raw", provider=fixture_provider)` → `list[tuple[date, list[TradeBar]]]`. This list is the single source of truth for both engines below.
3. **Run LEAN.** Construct `TrustedRunRequest(data_source="polygon", template="ema_crossover", symbol="SPY", start_ms_utc=..., end_ms_utc=..., bar_minutes=15, session="regular", adjustment="raw", starting_cash=100000)`. Inject the provider via dependency override at the test's monkey-patch boundary on `polygon_canonical.get_default_provider` (a single-line factory introduced for this purpose). `run_trusted_sample(...)` stages the fixture-derived bars, launches LEAN, parses normalized output.
4. **Run engine.** `algo = SpyEmaCrossoverAlgorithm(symbol="SPY")`; wrap `_on_fifteen_minute_bar` with a recording closure that, after each delegated call, appends a *copy* of `algo.last_decision_snapshot` to a list iff non-None. `engine = BacktestEngine(data_source=InMemoryDataReader(bars=flatten(bars_by_date)), fill_model=FillModel(mode=SIGNAL_BAR_CLOSE, commission_per_order=0))`. `engine.run(algo)`.
5. **Parse and assert.**
   - Parse LEAN's `<workspace>/output/storage/state.csv` into a list of dicts.
   - Parse engine's recorded snapshots into the same shape.
   - `assert_state_traces_match(lean_rows, engine_rows, atol=1e-9, rtol=0)` — first divergence prints both sides' full row (timestamp, all field values) and the field that broke tolerance.
   - `assert_trade_equivalence(lean_order_events, engine_order_events, fill_price_atol=Decimal("0.01"))` — runs only if state-trace passed. Asserts: equal trade count, equal timestamps (entry and exit), equal quantities, fill prices within tolerance.

A failure at step 5a points to indicator math or data drift; a pass at 5a with a failure at 5b points to decision logic or fill model.

## Manifest example (Polygon run)

```json
{
  "schema_version": 5,
  "data_policy": {
    "source": "polygon",
    "symbol": "SPY",
    "adjusted": false,
    "session": "regular",
    "input_bars":    {"timespan": "minute", "multiplier": 1},
    "strategy_bars": {"timespan": "minute", "multiplier": 15},
    "timestamp_policy": "bar_close_ms_utc",
    "timezone": "America/New_York",
    "fixture_id": "spy_minute_2025-01-06_2025-01-10",
    "fixture_sha256": "<64-hex>"
  },
  "fill_forward": false,
  "data_adjustment_policy": "pre_adjusted_non_reconciliation",
  "data_normalization_mode": "Raw",
  ...
}
```

## Fixture selection

The window must produce **at least one fresh-cross + gap + RSI-gated entry** under the EMA template on Polygon `raw` RTH-only bars. A zero-trade window degenerates the parity test to "0 trades == 0 trades", which proves data-path equivalence but not decision-path equivalence.

**Process to lock the window:**

1. Run `scripts/regenerate_polygon_fixture.py` against a candidate window. Start with Jan 6 – Jan 10, 2025 (matches the EMA template's existing default).
2. Run the LEAN EMA template against that fixture once outside the test harness. If LEAN produces ≥ 1 fully-closed round-trip trade, the window is acceptable; record the observed trade count in `attribution.md`.
3. If zero trades, advance the candidate window by 1 week and retry. Repeat until acceptance.
4. Commit the chosen window's fixture with `attribution.md` recording: window dates, bar count, observed trade count, observed first entry/exit timestamps, SHA-256 of `bars.json`, fetch timestamp, Polygon SDK version.

The spec deliberately does not bless Jan 6 – Jan 10 in advance. The implementation plan picks the window that proves out.

## Test surface

| Test | Tier | When it runs | What it asserts |
|---|---|---|---|
| `test_lean_engine_polygon_parity.py` | integration | Local + CI (when `LEAN_LAUNCHER_URL` set) | LEAN ≡ engine on per-bar state and trades |
| `test_polygon_fixture_freshness.py` | slow | `pytest -m slow` with `POLYGON_API_KEY` | Live Polygon ≡ recorded fixture |
| `test_polygon_canonical.py` | unit | Always | RTH filter, fail-fast on duplicates and non-monotonic |
| `test_ema_crossover_template.py` (extend existing) | unit | Always | AST-level: template references new params; writes `state.csv` |

## Error handling

- **Polygon fetch failure (production):** `LeanSidecarServiceError("polygon_fetch_failed: <upstream message>; window=<from>..<to>; symbol=<X>")`. Router maps to HTTP 502. No silent fallback to synthetic.
- **Empty Polygon response (production):** `LeanSidecarServiceError("polygon_returned_zero_bars: <window>; symbol=<X>")`. Same status. Fail-fast per the ingestion rule — a zero-bar window for a live request is data signal, not a recoverable empty.
- **Duplicate or non-monotonic Polygon timestamps:** `LeanSidecarServiceError("polygon_corrupt_timestamps: <details>")`. Per the numerical-rigor rule, do not silently `drop_duplicates` or reorder. The error message includes the first offending timestamp.
- **Fixture-window/parameter mismatch in tests:** `RecordedPolygonFixtureProvider` asserts `(symbol, from_date, to_date, adjusted)` match its `attribution.md`. A mismatch raises immediately so a test with the wrong window doesn't silently load the wrong bars.
- **EMA template warmup region:** `state.csv` row count must equal the post-warmup engine snapshot count. The parity test asserts equal row counts before per-row comparison; a count mismatch is its own failure class ("warmup region misaligned").
- **`bar_minutes != 15`:** parameter is wired, but only `15` is exercised by tests in this branch. The template raises `ValueError` if a parameter would cause inconsistent state (e.g., `EXIT_BARS` is pinned at 5 and depends on the consolidator period for its time-stop semantics — documented but not enforced in code, since `EXIT_BARS` is a spec constant).
- **Engine `last_decision_snapshot` None after warmup:** test's recording closure asserts non-None on post-warmup bars and surfaces the bar's `EndTime` in the error message.

## Open implementation choices (defer to writing-plans)

- Exact location of the `get_default_provider` factory (currently planned: `polygon_canonical` module; could equally live in `app/dependencies.py` if dependency-injection conventions prefer it).
- Whether `assert_state_traces_match` lives in `tests/_helpers/parity.py` or inline in the parity test. Probably the helper, for reuse by future strategies.
- Whether the manifest schema version bump should be `5` or a new `5.1` semver-style identifier — depends on what the existing schema-version policy is in `manifest.py` (writing-plans reads that file).
- Whether to add a `data_source` discriminator to the existing template-policy switch (`_BROKERAGE_POLICY_FOR_TEMPLATE` style) for symmetry, or keep `data_source` purely caller-driven.

## References

- `.claude/rules/numerical-rigor.md` — § "Timestamp rigor", § "Golden fixtures", § "External-API ingestion", divergence taxonomy.
- `.claude/skills/reconcile-backtest/SKILL.md` — divergence categories.
- `docs/architecture/lean-sidecar-lab.md` — workspace contract, launcher topology.
- `docs/superpowers/specs/2026-05-19-lean-ema-template-and-unified-history-design.md` — sibling design for the unified history (follow-up branch).
