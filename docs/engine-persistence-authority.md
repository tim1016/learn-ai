# Engine-side persistence authority

**Status:** canonical
**Domain:** in-process `BacktestEngine` runs writing to `StrategyExecution` + `BacktestTrade` (Postgres) through the same `.NET` persist endpoint as LEAN sidecar runs.
**Last reviewed:** 2026-05-19

## Why

Two engines produce backtest results in this repo:

- **LEAN sidecar.** Subprocess via the launcher, normalized result on disk, then POST to `/api/backtest-runs/persist-lean` (introduced by PR #291).
- **In-process spec strategies.** `SpecAlgorithm` driven through `BacktestEngine`. Until PR 4, these only populated a strategy-local `trade_log` and never reached the database.

For the unified history table (#294) and the cross-engine compare view (#295) to show engine-side runs alongside LEAN runs, in-process runs must persist through the same Postgres rows. The persist endpoint and payload are the shared contract; the engine path now uses them too.

The parity gate (`@pytest.mark.slow` in `PythonDataService/tests/integration/parity/`) closes the loop: it runs LEAN and the spec on the same SPY window, persists both, and asserts via GraphQL that the reconciled trade lists have zero divergences in the gating set from `.claude/rules/numerical-rigor.md` § "Trade-level reconciliation taxonomy".

## Layer-by-layer contract

### `.NET` side (`Backend/`)

- **`PersistLeanRunPayload`** (`Backend/Models/MarketData/PersistLeanRunPayload.cs`) — the shared persist payload. As of PR 4, `LeanRunId` is nullable.
- **`BacktestRunPersistenceService.PersistAsync`** (`Backend/Services/Implementation/`) — accepts `Source ∈ {"lean-sidecar","engine"}`. For `lean-sidecar`, `LeanRunId` is required and acts as the idempotency key (unique partial index on `(Source, LeanRunId) WHERE LeanRunId IS NOT NULL`, plus a race-condition catch on `SqlState 23505`). For `engine`, `LeanRunId` must be null and there is no idempotency — every persist creates a new row. `FillMode` is set to `"signal_bar_close"` for engine runs.
- **Endpoint:** `POST /api/backtest-runs/persist-lean` — same URL, both sources. Returns `{"strategy_execution_id": <int>}`.

### `PythonDataService` side

- **`app/services/lean_sidecar_persistence.py`** — LEAN path. `build_persist_payload` reads a normalized LEAN workspace and produces the dict. `persist_via_dotnet` posts it. (Pre-existing; unchanged in PR 4 except for `LeanRunId` nullability inherited from the model.)
- **`app/services/engine_persistence.py`** — engine path (PR 4). `EngineTrade` is the closed-round-trip shape with `quantity` (`Decimal`) and signed `pnl`. `compute_aggregates` rolls up KPIs. `build_engine_persist_payload` produces the same wire shape as the LEAN builder, with `source="engine"` and `lean_run_id=None`. `persist_engine_run` reuses `lean_sidecar_persistence.persist_via_dotnet` for transport.
- **`app/services/spec_strategy_runner.py`** — engine driver (PR 4). Loads a `StrategySpec`, runs it through `BacktestEngine` against a caller-provided `list[TradeBar]`, captures every `OrderEvent` via a thin `SpecAlgorithm` subclass (because the strategy's own `LoggedTrade` doesn't carry `fill_quantity`), pairs LONG/(SHORT|FLAT) fills into `EngineTrade` objects, and (optionally) persists.

### Pairing logic (`pair_engine_fills`)

In-scope strategies are long-only. The pairer:

- Treats `Direction.LONG` as entry.
- Treats `Direction.SHORT` OR `Direction.FLAT` as exit. The engine's force-flat path and bracket TP/SL exits emit `SHORT`; a strategy that explicitly liquidates to zero emits `FLAT`. Both paths exit the open LONG.
- Raises `NotImplementedError` on pyramiding (second LONG fill before exit).
- Raises `ValueError` on an unmatched exit or an event stream that ends with an open LONG. The engine's `on_force_flat` session-close hook should preclude the latter.
- Tags the trade as `is_synthetic_exit=True` when the exit's `OrderEvent.tag == "ForceFlat"`.

## Parity gate

**Location:** `PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py`

**Marker:** `@pytest.mark.slow`. Excluded from default CI runs. Run locally per the docstring in the file.

**Pinned reconciliation window** (update only with justification — LEAN image upgrade, fixture refresh, spec change — and regenerate the report):

- Symbol: `SPY`
- Start: `2025-01-06`
- End: `2025-01-10`
- Starting cash: `$100,000`

**Skip guards.** Test no-ops gracefully when any of:

- Backend (`http://backend:8080`) isn't reachable.
- `compareBacktestRuns` is missing from the GraphQL schema (stale `dotnet watch` build — `podman logs my-backend` for NuGet/dotnet errors).
- LEAN launcher process (`http://host.containers.internal:8090`) isn't running.
- No SPY zips under `/lean-cache` or `/lean-data` for the window.
- `PINNED_LEAN_IMAGE_DIGEST` is unset (run `scripts/lean_sidecar_pin_image.py` first).

**Acceptance gate.** Zero divergences in `GATING_CATEGORIES`:

```
DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH,
FILL_PRICE_DRIFT, ORDER_TYPE_MISMATCH, PNL_DRIFT, FIXTURE_INSUFFICIENT
```

**Output.** Every run writes a JSON snapshot to `/app/artifacts/parity-reports/ema_crossover_lean_vs_spec_<stamp>.json` (bind-mounted to `PythonDataService/artifacts/parity-reports/` on the host), pass or fail. The report is the authoritative human-reviewable artifact.

## Known unresolved category gaps

Two of the eight divergence categories from the taxonomy cannot be classified at the compare-service layer. Documented here for future closure:

- **`FIXTURE_INSUFFICIENT`** — requires price-history bar access to verify a fill price corresponds to a real bar. The compare service is pure-compute over trade lists; it doesn't have bars. The full `qc_reconciler._audit_fixture` performs this check upstream. Imported into the compare service for pass-through but never emitted at this layer.
- **`ORDER_TYPE_MISMATCH`** — requires LEAN order-type codes which are not in the round-trip trade payload (`PersistLeanTradePayload`). Adding it requires extending the DTO, the Postgres schema, and both engines' persist paths. Tracked as follow-up work; the parity gate currently cannot detect non-MARKET order types on either side.

All other six categories (DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH, FILL_PRICE_DRIFT, COMMISSION_DRIFT, PNL_DRIFT) are actively classified by `lean_sidecar_compare_service.reconcile_trade_lists`.

## Related files

| Layer | File | What it owns |
|---|---|---|
| .NET | `Backend/Models/MarketData/PersistLeanRunPayload.cs` | The shared persist payload contract |
| .NET | `Backend/Services/Implementation/BacktestRunPersistenceService.cs` | Idempotency + source-routing |
| .NET | `Backend/GraphQL/Comparison/CompareBacktestRunsResolver.cs` | The GraphQL endpoint the parity test queries |
| Python | `PythonDataService/app/services/engine_persistence.py` | Engine payload builder + POST |
| Python | `PythonDataService/app/services/spec_strategy_runner.py` | Load spec → run engine → capture trades → persist |
| Python | `PythonDataService/app/services/lean_sidecar_compare_service.py` | 6-of-8 category classifier |
| Python | `PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py` | The `@pytest.mark.slow` parity gate |
| Rules | `.claude/rules/numerical-rigor.md` | The taxonomy + gating set definition |

## Out of scope (follow-up tickets)

- Timestamp-rigor migration: persist `int64 ms UTC` instead of `timestamptz` for `ExecutedAt`/`EntryTimestamp`/`ExitTimestamp` and `bigint` instead of `varchar(20)` for `StartDate`/`EndDate`. Affects schema + payload + EF mappings + parity test guardrails (`sameWindow`). Tracked separately because of the migration weight.
- `ORDER_TYPE_MISMATCH` classifier extension (see above).
- Backfill CLI for historical on-disk LEAN runs — covered by PR 5 of the LEAN EMA crossover plan.
