# Engine-side persistence authority

**Status:** canonical
**Domain:** in-process `BacktestEngine` runs, LEAN sidecar runs and spec-strategy runs writing to the Python-owned run tables (`research_backtest_runs` + `research_backtest_run_trades`, Postgres) through one repository write.
**Last reviewed:** 2026-09-06 (PRD #1929 / ADR 0058 — the `.NET` persist endpoint is gone)

## Why

Two engines produce backtest results in this repo:

- **LEAN sidecar.** Subprocess via the launcher, normalized result on disk, then the canonical persist payload written through `app.research.backtest_runs.service.persist_run_payload` (the `.NET` `/api/backtest-runs/persist-lean` hop introduced by PR #291 was retired by PRD #1929).
- **In-process spec strategies.** `SpecAlgorithm` driven through `BacktestEngine`. Until PR 4, these only populated a strategy-local `trade_log` and never reached the database.

For the unified history table (#294) and the cross-engine compare view (#295) to show engine-side runs alongside LEAN runs, in-process runs must persist through the same Postgres rows. The persist payload and the repository write are the shared contract; the engine path uses them too.

The parity gate (`@pytest.mark.slow` in `PythonDataService/tests/integration/parity/`) closes the loop: it runs LEAN and the spec on the same SPY window, persists both, reconciles the persisted ledgers with the in-process classifier the parity verdict uses, and asserts zero divergences in the gating set from `.claude/rules/numerical-rigor.md` § "Trade-level reconciliation taxonomy".

## Layer-by-layer contract

### The write (`PythonDataService/app/research/backtest_runs/`)

- **`records.record_from_payload`** — the one converter from the canonical snake_case persist payload to the row. It enforces what the retired `.NET` writers enforced: `source ∈ {"lean-sidecar","engine"}`; for `lean-sidecar`, `lean_run_id` is required and is the idempotency key (unique partial index on `lean_run_id`; a redelivery returns the existing row and refuses a different `requested_engine`); for `engine`, `lean_run_id` must be null and there is no idempotency — every persist creates a new row; `fill_mode` defaults to `"signal_bar_close"` for engine runs.
- **`repository.insert_run`** — one transactional INSERT of the run row plus its trades. **`service.persist_run_payload` / `persist_run_payload_sync`** wrap it best-effort (a failure logs and yields `None`; the run that produced the payload is unaffected) and freeze the parity verdict after a LEAN companion lands.
- **Reads:** `GET /api/research/backtest-runs` and `GET /api/research/backtest-runs/{id}` (`app/routers/backtest_runs.py`).

### The producers (`PythonDataService/app/`)

- **`research/backtest_runs/engine_payload.py`** — the engine backtest path (Strategy Lab). `build_engine_run_payload` is a pure function of the engine response: per-trade dollar P&L under the executed fee policy, the strict dual-curve equity report, the frozen validation-analytics envelope.
- **`services/lean_sidecar_persistence.py`** — LEAN path. `build_persist_payload` reads a normalized LEAN workspace and produces the payload.
- **`services/engine_persistence.py`** — spec-runner path (PR 4). `EngineTrade` is the closed-round-trip shape with `quantity` (`Decimal`) and signed `pnl`. `compute_aggregates` rolls up KPIs. `build_engine_persist_payload` produces the same payload shape as the LEAN builder, with `source="engine"` and `lean_run_id=None`. `persist_engine_run` hands it to `persist_run_payload`.
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

- `POSTGRES_URL` is unset (the Python-owned run tables, and so both persists, need Postgres).
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
| Python | `PythonDataService/app/research/backtest_runs/records.py` | The canonical persist payload → row converter (validation, defaults) |
| Python | `PythonDataService/app/research/backtest_runs/repository.py` | The one write; LEAN idempotency; reads |
| Python | `PythonDataService/app/research/backtest_runs/parity.py` | The cross-engine parity verdict (trade reconciliation + receipts) |
| Python | `PythonDataService/app/services/engine_persistence.py` | Spec-runner payload builder |
| Python | `PythonDataService/app/services/spec_strategy_runner.py` | Load spec → run engine → capture trades → persist |
| Python | `PythonDataService/app/services/lean_sidecar_compare_service.py` | 6-of-8 category classifier |
| Python | `PythonDataService/tests/integration/parity/test_ema_crossover_lean_vs_spec.py` | The `@pytest.mark.slow` parity gate |
| Rules | `.claude/rules/numerical-rigor.md` | The taxonomy + gating set definition |

## Out of scope (follow-up tickets)

- Timestamp-rigor migration: persist `int64 ms UTC` instead of `timestamptz` for `ExecutedAt`/`EntryTimestamp`/`ExitTimestamp` and `bigint` instead of `varchar(20)` for `StartDate`/`EndDate`. Affects schema + payload + EF mappings + parity test guardrails (`sameWindow`). Tracked separately because of the migration weight.
- `ORDER_TYPE_MISMATCH` classifier extension (see above).
- Backfill CLI for historical on-disk LEAN runs — covered by PR 5 of the LEAN EMA crossover plan.
