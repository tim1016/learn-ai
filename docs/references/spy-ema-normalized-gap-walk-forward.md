# SPY EMA normalized-gap walk-forward

## Provenance

This research protocol was derived from the project discussion in the supplied [ChatGPT research conversation](https://chatgpt.com/c/6a7ff340-8f44-83ea-9ccf-bb8b13ca2eee) and then made executable under learn-ai's Python-owned numerical contract. The conversation is design provenance, not an independent numerical oracle. The formula fixture below checks exact `Decimal` arithmetic with independently declared inputs/expected values, but it implements the same algebra and is not an external reference oracle. The EMA and RSI implementations retain their existing LEAN-pinned references and parity tests.

## Frozen protocol

| Decision | Value |
|---|---|
| Protocol identity | `spy-ema-normalized-gap`, version `1.0`; persisted on every canonical walk-forward config |
| Instrument | SPY |
| Bars | 15-minute, regular NYSE session bars supplied by the default `LeanMinuteDataReader(session="regular")` |
| Entry | Fresh EMA(5) cross above EMA(10), normalized gap at or above the selected threshold, and `50 <= RSI(14) <= 70` |
| Exit | Five consolidated bars after entry |
| Position | Long-only, `SetHoldings(1.0)`, pyramiding 1 |
| Control | Existing absolute EMA gap of `$0.20` over the full study window |
| Candidate thresholds | `1, 2, 3, 4, 5, 7.5, 10` basis points, ascending declaration order |
| Selection | Highest eligible train Sharpe; tie by train total return, then declaration order |
| Eligibility | At least five train trades and a non-null train Sharpe |
| Walk-forward | Rolling 180 calendar-day train, 30 calendar-day test, 30 calendar-day step |
| Default window | 2024-08-01 through 2026-08-01; 18 OOS folds |
| Fill and costs | Next-bar-open; zero commission and slippage baseline |
| Fold state | Indicators and `FreshCross` pre-roll from the fold's train start; TEST positions start and end flat |
| Execution boundary | Cancellable background job `spy_ema_walk_forward`; clients cannot override V1 parameters |

The control is a comparison receipt, not a candidate. It cannot win a fold. Every threshold candidate is run only on its fold's training window. The selected candidate's fully materialized `StrategySpec` is then frozen and run only on the following test window.

At pipeline start, Python resolves the data-root revision once and passes that exact revision to the control, all training candidates, and all TEST children. Snapshot IDs still encode each child's own data window, but their revision component cannot drift during the 145-run protocol.

## Normalized-gap formula

For fast EMA value `f` and slow EMA value `s`:

```text
difference_bps(f, s) = 10,000 * (f - s) / s
```

The denominator must be non-zero. Indicator warmup propagates `None`, so the comparison cannot fire until both operands are ready.

- Canonical implementation: `PythonDataService/app/engine/strategy/spec/primitives.py::evaluate_operand`, `DifferenceBps` branch.
- Serialized AST: `PythonDataService/app/engine/strategy/spec/schema.py::DifferenceBps`.
- Golden input/output: `PythonDataService/tests/fixtures/golden/spy-ema-difference-bps/`.
- Tolerance: exact `Decimal` equality (`rtol = 0`, `atol = 0`). The expected strings are generated independently from the evaluator.
- Validating test: `PythonDataService/tests/engine/strategy/spec/test_difference_bps_operand.py`.

## Selection formula

For eligible training candidates in declaration order, choose:

```text
argmax (train_sharpe, train_total_return, -declaration_index)
```

The declaration grid is ascending, so a complete metric tie selects the lower threshold. A candidate is ineligible if its run failed, it produced fewer than `min_train_trades`, or its Sharpe is null. If no candidate remains, selection raises and the walk-forward fails without a test run.

- Canonical implementation: `PythonDataService/app/research/walk_forward/selection.py::select_candidate_index`.
- Validating test: `PythonDataService/tests/research/walk_forward/test_selection.py`.
- Orchestration/lineage tests: `PythonDataService/tests/research/walk_forward/test_runner.py` and `test_spy_ema.py`.

## Retention formula

The absolute `$0.20` control is a different strategy and is never used as the normalized search's retention denominator. For every completed parameter-search fold with non-zero selected training Sharpe:

```text
fold_retention = test_sharpe / selected_winner_train_sharpe
oos_retention = arithmetic_mean(eligible fold_retention values)
```

Fold ratios receive equal weight. This is intentionally not `mean(test Sharpe) / mean(train Sharpe)`, which answers a different question. `oos_retention_basis = "mean_fold_test_to_selected_train"` makes the wire meaning explicit. The pinned formula test uses fold pairs `(1/2, 9/3)` and expects `(0.5 + 3.0) / 2 = 1.75`, not the ratio-of-means value `2.0`.

## Fold-boundary state

The TEST child reads from `train_start_ms`, and its ledger records that earlier boundary as `warmup_start_ms`. All indicators and stateful entry primitives observe pre-roll bars, while entry orders remain disabled until `test_start_ms`. Persisted metrics, curves, trades, and consumed-bar counts exclude pre-roll. Each TEST starts flat and is forced flat at its end; the combined curve compounds independently-flat fold returns rather than claiming continuous position ownership.

## Receipt lineage

The full-window absolute-gap control is persisted first. Its `run_id` becomes the walk-forward `parent_run_id`. Every train candidate and frozen test run is a normal child `RunLedger` with `parent_run_id = walk_forward_id`. The walk-forward config stores the protocol identity/version, flat-boundary policy, candidate assignments, spec hashes, and full spec JSON; each fold stores all training metrics, eligibility reasons, the selected parameters, selected train Sharpe, fold-local retention, and the test run ID.

Selection failure retains the failing fold's complete train-side evidence in `selection_failures[]`. A test receipt that cannot be persisted gets a null `test_run_id`; that fold and its curve are excluded from aggregation, and the overall walk-forward fails closed.

A TEST child that consumes zero reported-window bars also fails its fold closed, even if train-side pre-roll bars were available. Its persisted receipt and warning remain inspectable, but it contributes no curve or aggregate metric. If no TEST fold completes, the aggregate status is `failed` rather than a completed zero-return study.

Persisted-page discovery starts with the newest exact protocol-ID/version receipt and follows its `parent_run_id` to the control. A newer manual or orphaned control therefore cannot hide or relabel a valid canonical study.

This makes the UI a renderer of Python-authored evidence. Angular may format and chart the values, but it does not calculate thresholds, choose candidates, compound returns, or derive statistics.
