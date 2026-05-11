# Phase 3 implementation summary — what shipped vs. what's still open

Date: 2026-05-11
Branch: `feat/phase3-pnl-parity`
PR: https://github.com/tim1016/learn-ai/pull/218
Predecessor specs:
- `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`
- `docs/superpowers/plans/2026-05-11-phase3-pnl-parity.md`

This doc records exactly what Phase 3 implemented in this PR, and what
remains for the **fixture-landing PR** (Tim captures QC artifacts) and
the eventual **Phase 4** (multi-symbol top-N ranking). The split is
deliberate — Phase 3 as scaffolded here is end-to-end testable against
synthetic QC-like payloads and unblocks fixture capture without
requiring its prerequisites.

## What shipped in this PR

### Code (under `PythonDataService/app/research/parity/`)

| File | Purpose | Lines | Tests |
|---|---|---|---|
| `__init__.py` | Package marker | 3 | — |
| `fixture_data_reader.py` | CSV-backed daily-bar reader; yields engine-shaped `TradeBar` anchored 09:30→16:00 ET. Plugs into `run_strategy_spec`'s existing `data_source_factory` via `fixture_data_source_factory`. | ~130 | 7 unit tests |
| `ibkr_commission.py` | Standalone IBKR equity-tier commission calc ($0.005/share, $1 floor, 0.5% cap). Not wired into the engine. | ~70 | 8 unit tests |
| `qc_reconciler.py` | `reconcile_qc_aapl_phase3` public entry, four private steps (parse / audit / align / classify), 8-category `DivergenceCategory` `StrEnum`, typed `ReconciliationReport` with markdown + JSON renderers. | ~430 | 18 unit tests |

### Tests (under `PythonDataService/tests/research/parity/`)

| File | What it covers | Activation |
|---|---|---|
| `test_fixture_data_reader.py` | CSV → TradeBar shape, NY session anchoring, date-range filtering, symbol filtering (case-insensitive), `bar_open_by_date` accessor, factory adapter. | runs every PR |
| `test_ibkr_commission.py` | Per-share computation, $1 floor, 0.5% cap, negative-quantity handling, zero edge cases, AAPL-representative fill ($2.63 for 526 shares @ $190), custom-rate override. | runs every PR |
| `test_qc_reconciler.py` | Each private step independently (parse, audit, align, classify) + 4 end-to-end happy/failure paths through `reconcile_qc_aapl_phase3`. | runs every PR |
| `conftest.py` | Registers `--write-recon-report` flag scoped to this dir. | always loaded |
| `test_qc_fixture_smoke.py` | Validates QC fixture event-field shape; logs `FEE_PRESENCE_BRANCH=A\|B` (decides whether `assert_fees=True` is valid). | **skipped** until fixture lands |
| `test_qc_aapl_phase3_trade_parity.py` | Acceptance test wired to the reconciler; only `_build_our_fills` left to implement. | **skipped** until fixture lands |

### Documentation

| File | Purpose |
|---|---|
| `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md` | Locked design (architecture diagram, four sub-sections, divergence taxonomy, acceptance gates, escalation paths, open risks). |
| `docs/superpowers/plans/2026-05-11-phase3-pnl-parity.md` | TDD implementation plan with bite-sized tasks. |
| `docs/references/qc-aapl-phase3-capture-runbook.md` | Step-by-step QC Cloud capture script Tim runs to produce the fixture. Includes algorithm code with AAPL-only universe override, three API pulls (orders / price history / equity), attribution template. |
| `.claude/rules/numerical-rigor.md` (extended) | Trade-level reconciliation taxonomy section: 8 `DivergenceCategory` values, what each means, which route to Phase 3 engine fix vs. Phase 3.5 escalation, gating-vs-diagnostic distinction. |
| `docs/references/reconciliations/.gitkeep` | Reserved directory for hand-authored reconciliation summaries (one per accepted Phase 3 reconciliation). |

### Reused without modification

- `FillMode.NEXT_BAR_OPEN` at `app/engine/execution/order.py:36` and its
  implementation in `app/engine/execution/fill_model.py:77-83` already
  satisfy the Phase 3 formal rules (signal-at-T-close → fill-at-T+1-open,
  defer-when-next-bar-missing, market-orders-only, commission propagation).
  Covered by `tests/engine/test_fill_model.py`. No new code required.
- `RunRequest.fill_mode: str` already accepts `"next_bar_open"`
  (`runner.py:80,118`). No signature change required.
- `app/research/runs/runner.py::run_strategy_spec` accepts the
  `data_source_factory` callable that `fixture_data_source_factory`
  satisfies. No runner changes required.

## What was NOT shipped (deferred — by design)

### Out of this PR, but in scope for the fixture-landing PR

1. **The QC fixture itself** — `tests/fixtures/golden/qc-aapl-phase3/`
   (5 files: `qc_orders.json`, `qc_price_history.csv`, `qc_equity.json`,
   `qc_algorithm_screenshot.png`, `attribution.md`). Tim captures these
   from QC Cloud following the runbook.
2. **`_build_our_fills` body** in
   `tests/research/parity/test_qc_aapl_phase3_trade_parity.py`. Currently
   `raise NotImplementedError(...)`. The body needs to:
   - Build the AAPL single-symbol `StrategySpec` from spec §2.3.
   - Construct `RunRequest` with `fill_mode="next_bar_open"`,
     `commission_per_order=0`.
   - Call `run_strategy_spec` with `fixture_data_source_factory(...)`.
   - Adapt each `LoggedTrade` into a pair of `OurFill` records
     (entry side + exit side), computing fee via
     `IbkrEquityCommissionModel.fee(...)`.
3. **The hand-authored reconciliation summary** at
   `docs/references/reconciliations/qc-aapl-phase3.md` — written only
   after the acceptance test reaches `status="passed"` (or after the
   first round of fixes lands).

### Out of Phase 3 entirely — Phase 3.5 escalations

These activate only on specific divergence types observed in the first
reconciliation run:

| Trigger divergence | Escalation work |
|---|---|
| `FILL_PRICE_DRIFT` clustered | Port LEAN's `EquityFillModel` (partial fills, halts, gap-auction logic). |
| `FILL_PRICE_DRIFT` on the first bar of a position only | Re-capture fixture at minute resolution; daily bars insufficient. |
| `COMMISSION_DRIFT` (Branch B → Branch A promotion) | Wire `IbkrEquityCommissionModel` into the engine via a `CommissionModel` protocol so trades carry per-fill fees in `LoggedTrade` extensions. |
| `FIXTURE_INSUFFICIENT` (only one or two outliers) | Re-examine the affected trading dates manually; usually a corporate action or a halt in the data that wasn't captured. |

### Out of Phase 3 entirely — Phase 4

The full multi-symbol top-N ranking algorithm from QC's tutorial. This
needs a `PortfolioConstruction` extension to `StrategySpec` (rank by
prediction value, take the top-N, equal-weight). Unrelated to Phase 3
acceptance.

## Acceptance gate status

| Gate item | Status |
|---|---|
| Design spec committed | ✅ |
| Implementation plan committed | ✅ |
| FixtureDataReader implemented + tested | ✅ |
| FillMode.NEXT_BAR_OPEN behavior verified | ✅ (no new code; uses existing impl) |
| IbkrEquityCommissionModel implemented + tested | ✅ |
| QcReconciler + ReconciliationReport implemented + tested | ✅ |
| Pytest `--write-recon-report` flag registered | ✅ |
| Smoke + acceptance tests committed (skipped until fixture) | ✅ |
| Numerical-rigor doc updated with divergence taxonomy | ✅ |
| QC fixture captured | ⏳ Tim — see runbook |
| `_build_our_fills` implemented | ⏳ fixture-landing PR |
| First reconciliation run produces `status="passed"` | ⏳ fixture-landing PR |
| Reconciliation summary at `docs/references/reconciliations/qc-aapl-phase3.md` | ⏳ post-acceptance |

## Test-suite hygiene

Project-scope test run (`pytest /app/tests -q --ignore=/app/tests/integration --ignore=/app/tests/fixtures/test_golden_manifest.py -k "not slow"`):

```
2504 passed, 30 skipped, 2 deselected, 5 xpassed, 1 failed
```

The one failure is `tests/broker/ibkr/test_config.py::test_defaults_are_paper_on_paper_port`,
which **also fails on master** — the polygon-data-service container has
`IBKR_CLIENT_ID=42` in its env, overriding the test's expected default
`1`. This is pre-existing infrastructure noise, not a regression from
this PR. Surfaced in the PR description.

`tests/fixtures/test_golden_manifest.py` has a collection error
(missing `jsonschema` module in the container) — also pre-existing.

## How to activate Phase 3 from here

1. Tim opens QC Cloud and follows
   `docs/references/qc-aapl-phase3-capture-runbook.md` end-to-end.
2. Drops the captured five files into
   `PythonDataService/tests/fixtures/golden/qc-aapl-phase3/`.
3. Runs:
   ```
   podman exec polygon-data-service python -m pytest \
     /app/tests/research/parity/test_qc_fixture_smoke.py -v
   ```
   The smoke test logs `FEE_PRESENCE_BRANCH=A|B`. Records which branch
   in the fixture's `attribution.md`.
4. Opens a new PR (fixture-landing) that:
   - Commits the five fixture files.
   - Replaces `raise NotImplementedError` in
     `test_qc_aapl_phase3_trade_parity.py::_build_our_fills` with the
     real engine-replay implementation.
   - If Branch A, flips `assert_fees=True` in the acceptance test.
5. Runs the acceptance test. If `status="passed"`, writes the
   reconciliation summary. If `status="failed"`, the
   `--write-recon-report` flag dumps a human-readable divergence list
   to `PythonDataService/artifacts/reconciliations/qc-aapl-phase3-latest.md`
   that classifies each failure into the taxonomy.

## Files touched

```
.claude/rules/numerical-rigor.md
PythonDataService/app/research/parity/__init__.py
PythonDataService/app/research/parity/fixture_data_reader.py
PythonDataService/app/research/parity/ibkr_commission.py
PythonDataService/app/research/parity/qc_reconciler.py
PythonDataService/tests/research/parity/__init__.py
PythonDataService/tests/research/parity/conftest.py
PythonDataService/tests/research/parity/test_fixture_data_reader.py
PythonDataService/tests/research/parity/test_ibkr_commission.py
PythonDataService/tests/research/parity/test_qc_aapl_phase3_trade_parity.py
PythonDataService/tests/research/parity/test_qc_fixture_smoke.py
PythonDataService/tests/research/parity/test_qc_reconciler.py
docs/references/qc-aapl-phase3-capture-runbook.md
docs/references/reconciliations/.gitkeep
docs/superpowers/plans/2026-05-11-phase3-pnl-parity.md
docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md
```

## Commit log

```
d9a4266 docs(parity): phase 3 design spec + implementation plan
7bdd37b chore(parity): scaffold app/research/parity package + reconciliation docs dir
42de524 feat(parity): FixtureDataReader for CSV-backed daily-bar replay
a5dad81 feat(parity): IbkrEquityCommissionModel for reconciler-side fee parity
7338165 feat(parity): QcReconciler — diff QC trade log vs ours with typed taxonomy
8c21d9a test(parity): conftest flag + skipped phase-3 smoke/acceptance tests
d3c4b6e docs(rules): trade-level reconciliation divergence taxonomy
bad79df docs(parity): QC AAPL Phase 3 fixture capture runbook for Tim
```
