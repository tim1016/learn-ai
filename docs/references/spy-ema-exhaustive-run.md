# SPY EMA Exhaustive Run

**Protocol:** `spy-ema-exhaustive-run` version `1.0`

**Purpose:** Turn the candidate evidence already persisted by canonical SPY EMA
walk-forward V1 into a compact comparison set. The protocol intentionally
keeps two different questions separate:

1. How did each selected gap behave over the full available two-year sample?
2. How stable was that same fixed gap across the 18 forward one-month tests?

The first track is descriptive and selection-biased. The second track is the
forward-validation evidence. A full-period winner must never be relabeled OOS.

## Frozen source and window

The only accepted source is a completed `spy-ema-normalized-gap` V1.0 receipt
covering the half-open interval 2024-08-01 through 2026-08-01 with all 18
canonical rolling folds. The source's strategy specifications, training
receipts, fill model, costs, initial cash, and data revision are reused.

V1 therefore retains the source limitation of zero commission and zero
slippage. This keeps the numbers comparable, but it is not a realistic-cost
promotion test.

## Candidate selection

Only eligible persisted TRAIN outcomes participate. Within each fold, define
the tie-aware empirical percentile ranks of training Sharpe and training net
return among that fold's eligible candidates. The score is:

```text
selection_score = 0.5 × percentile(train Sharpe)
                + 0.5 × percentile(train net return)
```

The protocol keeps at most five candidates from each fold. Ties are resolved by
training Sharpe, training return, trade count, and then the earlier declared
grid position. Test-window information never enters this ranking.

Exact strategy-spec hashes are deduplicated after selection. A gap selected in
several folds is run once per evidence track, while its selection count, fold
indices, best rank, latest selected fold, and mean selection score remain in the
final row.

Trade count is not rewarded in the composite score. It already acts as an
eligibility/confidence floor in the source protocol. Max drawdown is displayed
but deliberately excluded from V1 ranking.

## Evidence tracks

### Full two-year fit — explicitly not OOS

Each selected unique specification runs once over the entire frozen two-year
window. The output supplies net return, Sharpe, max drawdown, total trades, and
trade-recency statistics. Because the specification was selected after
inspecting folds inside this same history, these values contain selection and
look-ahead bias. They are suitable for description and sample-size checks, not
for proof of generalization.

Net return is the engine's `total_return_pct`:

```text
net return = (final equity − initial cash) / initial cash
```

It is the closest table measure to portfolio growth. Sharpe instead measures
average return relative to return volatility; it does not represent the rate
of portfolio growth.

### Fixed-gap forward stability — OOS

Each selected unique specification also runs through the source's exact 18
rolling 180-day TRAIN / 30-day TEST folds. The gap remains fixed across the
study; it is not reselected. Indicator state is pre-rolled from TRAIN, while
positions and reported metrics remain TEST-only and flat at fold boundaries.

The candidate row reports:

- percent of completed test folds with positive net return;
- arithmetic mean and median of non-null fold test Sharpes;
- ordinary least-squares slope of fold Sharpe against fold index (`alpha_decay`);
- arithmetic mean of eligible fold-local Sharpe retentions.

For this fixed candidate, fold retention is:

```text
fold retention = candidate TEST Sharpe / same candidate TRAIN Sharpe
```

Folds with null TEST Sharpe or zero/null TRAIN Sharpe do not contribute to the
mean. The UI exposes all 18 fold rows and links each auditable TEST receipt.

## Trade recency

Completed trades are classified by exit timestamp. The recent window is the
final six calendar months, 2026-02-01 through 2026-08-01.

```text
recent share = recent completed trades / all completed trades

recent/prior rate ratio
  = (recent trades / recent-window duration)
    / (prior trades / prior-window duration)
```

A ratio above 1 means trades occurred more frequently per unit of calendar time
in the last six months; below 1 means less frequently. The ratio is null when
the earlier period has no trades, because division by a zero prior rate would
not be meaningful. The last completed-trade exit date is also persisted.

## Execution and persistence

The public background job is `POST /api/jobs/spy_ema_exhaustive`. Its only
caller-supplied parameter is the 32-hex source `walk_forward_id`; .NET mints the
job identity and Python owns all selection, statistics, and persistence.

The immutable aggregate is stored at:

```text
<artifacts_root>/exhaustive-run/<exhaustive_run_id>/
├── config.json
└── result.json
```

Read endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/research/exhaustive-runs/{exhaustive_run_id}` | Load one immutable result |
| `GET` | `/api/research/exhaustive-runs/by-walk-forward/{walk_forward_id}` | Load the newest linked result |

Every full-period child is a normal strategy-run receipt. Every fixed-gap OOS
track is a normal persisted walk-forward receipt. The aggregate links both, so
the sortable table is a projection of Python-authored evidence rather than a
frontend calculation.

## Numerical provenance

| Concept | Canonical implementation | Validation |
|---|---|---|
| Fold candidate score and deduplication | `PythonDataService/app/research/exhaustive_run/selection.py` | `PythonDataService/tests/research/exhaustive_run/test_selection.py` |
| Trade-recency share and rate ratio | `PythonDataService/app/research/exhaustive_run/metrics.py` | `PythonDataService/tests/research/exhaustive_run/test_exhaustive_metrics.py` |
| Fold-local Sharpe retention | `PythonDataService/app/research/walk_forward/metrics.py` | `PythonDataService/tests/research/walk_forward/test_metrics.py` |
| Full orchestration and lineage | `PythonDataService/app/research/exhaustive_run/runner.py` | `PythonDataService/tests/research/exhaustive_run/test_runner.py` |

This protocol is repository-internal research design, not an external strategy
claim. It is for research and education and is not financial advice.
