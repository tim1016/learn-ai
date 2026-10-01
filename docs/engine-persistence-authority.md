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

## Known unresolved category gaps

Two of the eight divergence categories from the taxonomy cannot be classified at the compare-service layer. Documented here for future closure:

- **`FIXTURE_INSUFFICIENT`** — requires price-history bar access to verify a fill price corresponds to a real bar. The compare service is pure-compute over trade lists; it doesn't have bars. The full `qc_reconciler._audit_fixture` performs this check upstream. Imported into the compare service for pass-through but never emitted at this layer.
- **`ORDER_TYPE_MISMATCH`** — requires LEAN order-type codes which are not in the round-trip trade payload (`PersistLeanTradePayload`). Adding it requires extending the DTO, the Postgres schema, and both engines' persist paths. Tracked as follow-up work; the parity gate currently cannot detect non-MARKET order types on either side.

All other six categories (DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH, FILL_PRICE_DRIFT, COMMISSION_DRIFT, PNL_DRIFT) are actively classified by `lean_sidecar_compare_service.reconcile_trade_lists`.
