# Strategy Lab regression investigation and repair — 2026-09-27

## Outcome

EMA Crossover Signal and RSI Mean Reversion complete from the actual Strategy
Lab UI in Python, standalone LEAN, and Both mode. Both mode reports **Agree**
for each strategy on the historical three-month and six-month SPY windows.
The historical parity scope and tolerances are unchanged.

The failures were integration regressions around stronger data-integrity
checks. **No Polygon historical-quotes subscription is required by these
paths.** LEAN quote archives are synthesized locally from captured Polygon
trade OHLCV; they do not represent real historical NBBO quotes. The synthetic
quote implementation and all strategy/statistics mathematics remain unchanged.

Investigation began at `ac0c23461f60f08388cbc4495a8b6c91681baad8`; the repair
branch is based on `e08dc4ea` (master). Validation used the running local
frontend, backend, Python service, lake/catalog, and real pinned LEAN containers.
Saved run IDs below belong to this local development database, not a portable
production deployment.

## Proven causes and repairs

| Failure | Evidence and cause | Repair |
| --- | --- | --- |
| Adjusted standalone LEAN refuses `lake_adjustment_version_mismatch` | PR [#2513](https://github.com/tim1016/learn-ai/pull/2513), commit `7d8e8503`, began verifying corporate-action versions for quotes as well as trades. SPY's 62 adjusted trade archives had version receipts; its 62 existing synthetic quote archives had none. Python/instrument preparation requests trades only. The same files pass the preceding resolver and fail the current resolver. | A live Polygon LEAN run gets one canonical `ensure_data` preparation attempt for repairable preflight failures, requesting trade and quote artifacts. The writer derives quotes from admitted trades and publishes valid version receipts. Preparation happens outside the adjusted run lock; launch reacquires it and repeats full admission/version checks. |
| Both fails before launching LEAN with a worker-thread exception | PR [#2516](https://github.com/tim1016/learn-ai/pull/2516), commit `243efdad`, added catalog-backed committed admission. Normal preflight was moved to a worker; the separate frozen `bar-store-v1-*` branch still called the reader on the async event loop. Controlled probes returned the same 390 bars with the old reader and the current worker-thread reader; the current direct async call raised. | Move snapshot validation and minute reads together to a worker thread. Preserve both the source/staged fixture hashes and catalog admission. Frozen inputs are never repaired or refreshed by the new live preparation path. |
| New admission also refuses older metadata | Actual UI execution, after quote repair, exposed complete metadata rows with NULL adjustment scope. Older writers encoded the mode in the data-contract hash but omitted the row column. Market-hours and symbol-properties bytes were otherwise valid. | The verified metadata bundle can fill that NULL scope only on the exact complete row selected by its mode-bound contract hash, matching root, path, content hash, and size. Wrong scope, different bytes/root, or non-complete rows remain refused. Readers do not infer scope. |
| Successful lake-backed runs cannot verify native statistics | The August 8 verifier (`a50675d0`) expects a private workspace rate file; the later lake mount left it absent. An isolated probe with the exact manifest-hashed rate input reproduced 66 native and 25 formatted values. | Retain the admitted rate-file bytes in the private workspace before execution. LEAN still uses the read-only lake mount; the unchanged verifier and later historical verification can read this run's retained input. |
| Switching a completed Both run to standalone LEAN fails `compatibility_fixture_source_mismatch` | Reproduced in the UI after RSI Both completed. `changeEngine()` changed raw to adjusted while retaining the raw frozen fixture ID/hash. | Release the automatically captured Polygon bar-store fixture when an explicit engine change changes adjustment mode. The next run resolves the newly selected tree. An unchanged Both replay retains its frozen receipt. |

The first two protections addressed real numerical-integrity defects and remain
enabled. No guard, tolerance, or golden fixture was weakened. The metadata
repair cannot bless arbitrary legacy files: it starts with the existing
image-bound verified bundle and a matching committed catalog row.

Implementation: [sidecar orchestration](../../PythonDataService/app/services/lean_sidecar_service.py),
[metadata bundle](../../PythonDataService/app/data_lake/metadata_bundle.py),
[catalog update](../../PythonDataService/app/data_lake/catalog_client.py), and
[UI configuration](../../Frontend/src/app/components/strategy-lab/strategy-lab-config.store.ts).

## Polygon data lineage

The lake [fetcher](../../PythonDataService/app/data_lake/polygon_fetcher.py)
requests minute aggregate OHLCV. The canonical
[writer](../../PythonDataService/app/data_lake/ensure_data.py) reads committed
same-day trades and invokes the local
[quote builder](../../PythonDataService/app/data_lake/derived_quote.py).
The builder copies trade open/high/low/close into both bid and ask OHLC, with
zero sizes. Its executable behavior dates to `775672bd` (May 21); September's
integrity changes did not introduce a real-quotes dependency.

The regression tests exercise the actual repair writer and assert no new
minute-aggregate fetch when the admitted trades are already present. Ordinary
corporate-action/aggregate requests may still be needed for missing or stale
trade inputs. There is no Polygon historical-quotes endpoint call in this path.
The user's account billing entitlements were not inspected or changed.

Legacy private-workspace staging uses trade close for every synthetic quote
OHLC field, whereas lake quote derivation copies each respective trade OHLC.
That older distinction remains outside a universal quote-high/low parity claim;
Both mode's frozen execution contract is the authority for its comparison.

## Actual UI validation

All runs selected SPY, regular-session minute inputs, fifteen-minute strategy
bars, default strategy parameters, and starting cash of $100,000. W3mo is
2026-02-02 through 2026-04-30; W6mo is 2025-11-03 through 2026-04-30.

| Strategy | Window / mode | Saved run(s) | Trades | Closed-trade net P&L | Fees | UI outcome |
| --- | --- | --- | ---: | ---: | ---: | --- |
| EMA Crossover Signal | W3mo Python | 26 | 11 | $157.9004 | $0 | Completed |
| EMA Crossover Signal | W3mo LEAN | 23 | 11 | $133.3254 | $22 | Completed |
| EMA Crossover Signal | W3mo Both | 24 / 25 | 11 / 11 | $133.3254 | $22 | Agree |
| RSI Mean Reversion | W3mo Python | 30 | 8 | $5,125.16 | $0 | Completed |
| RSI Mean Reversion | W3mo LEAN | 29 | 8 | $5,037.99 | $16 | Completed |
| RSI Mean Reversion | W3mo Both | 27 / 28 | 8 / 8 | $5,008.60 | $16 | Agree |
| EMA Crossover Signal | W6mo Both | 33 / 34 | 20 / 20 | $2,696.8751 | $40 | Agree |
| RSI Mean Reversion | W6mo Both | 31 / 32 | 15 / 15 | $7,286.6646 | $30 | Agree |

Open `/strategy-lab?run=<id>` in the local UI to inspect a result. Each Both
receipt shows matching shared inputs and LEAN-native values. The W3mo RSI
Both report was reloaded, switched to LEAN, and successfully produced run 29,
then switched to Python and produced run 30. This exercises the restored-run
transition that previously failed, not just fresh configurations.

Standalone defaults use adjusted data and their own execution policies;
Python's submitted flat commission was zero. Both uses raw data with the
pinned `us-equity-raw-ibkr-v1` execution contract. Different standalone P&L is
not an equivalent-input parity failure. The table reports the shared
closed-trade ledger, not necessarily native portfolio equity including other
cash flows.

## Historical parity scope and numerical receipts

The existing [EMA receipt](../references/reconciliations/ema-crossover-signal-lean-2026-07-18.md)
requires exact observations, strategy-state tolerance `1e-9`, and reconciled
orders. The existing [RSI receipt](../references/reconciliations/rsi-mean-reversion-lean-2026-09-01.md)
provides the trade-level contract and ENG-009 fixture; it explicitly does not
claim an RSI per-bar state matrix. Those boundaries are preserved.

All four live Both pairs have zero classified divergences, matching program
versions/parameters, and identical input fixture hashes:

- W3mo: `0eb06aa97f4e9159b4b73a89dbad29b551922930611065a0c39cbaab754039e4`.
- W6mo: `e89d2b230a31d8dfdd5617a19088686e84c9671145cba54c459c2b848f359bd9`.

These are the same hashes recorded by the historical RSI receipt. RSI also
reproduces its recorded 8/15 trades, 5/10 wins, $5,008.60/$7,286.6646 net P&L,
and $16/$30 fees. EMA reproduces the historical SPY 22/40 filled-order counts.
The existing observation/state/trade golden matrix was rerun separately.

The persisted live verdict retains its existing fill-price tolerance of $0.01.
An additional comparison of the saved paired trade ledgers checks entry/exit
timestamps and quantities exactly and prices/net trade P&L at absolute
`1e-6`, relative zero. This is diagnostic verification, not a changed contract.

All six real LEAN executions (two standalone plus four companions) reproduce
**66 native metrics and 25 formatted metrics with zero divergences**, using
`lean-native-statistics-v2-261366a7e26ae942df858ab20df4fef8fa07de67`
and existing absolute tolerance `0.0000500001`.

Readiness remains explicitly **unavailable for comparison** with
`readiness_statistics_basis_differs`: Python and LEAN grade different bases.
Standalone LEAN's UI still shows incomplete Backtest Evidence Grade (4/17
inputs), while its native-statistics verification matches. Neither readiness
formula nor completeness contract is changed or claimed as universal parity.
No new claims cover real bid/ask spreads, arbitrary strategy parameters,
additional symbols, live execution, or RSI per-bar state equivalence.

## Regression and validation evidence

New tests first reproduced the actual failure, then passed with the repair:

- Real managed-lake frozen reads through the async orchestrator, including
  refusal after committed receipts are revoked.
- Canonical repair of missing quote files, missing quote adjustment receipts,
  legacy metadata scope, and combined damage; no redundant aggregate fetch;
  full revalidation and no nested capture-lock deadlock.
- Exact retained rate bytes/hash and independence from a later lake update.
- PostgreSQL scope update: exact committed file accepted; wrong root, hash,
  size, path, existing scope, or publication status refused.
- Frontend outgoing requests after Both-to-LEAN and Both-to-Python changes;
  unchanged Both replay retains its receipt.

Completed checks (overlapping suites are not additive):

| Check | Result |
| --- | --- |
| Bounded Python fast gate, including sidecar regressions | 8,584 passed, 20 skipped, 1 expected failure; 82.14 seconds |
| Catalog write and metadata bundle tests on disposable PostgreSQL | 87 passed |
| Historical EMA/RSI/compatibility golden suites and cross-engine matrix | 19 passed, 6 missing-fixture skips |
| Strategy Lab frontend suite | 155 passed across 16 files |
| Full Python app/tests Ruff and full frontend ESLint | Passed |
| Diff whitespace check | Passed |

The six matrix skips are pre-existing absent cells: SPY/QQQ W24mo and
AAPL/TSLA W3mo/W24mo. The historical SPY and QQQ W3mo/W6mo cells ran and passed.
Golden reference files were not regenerated. Existing unrelated Angular
compiler warnings (unused DecimalPipe and unnecessary nullish coalescing)
remain outside this repair.

Detailed local API responses, initial read-only probes, and the original
pre-repair investigation are retained under the ignored directory
`PythonDataService/artifacts/strategy-lab-validation-2026-09-27/`.
The committed regression tests and historical fixtures provide reproducible
coverage independent of that local directory.

## Operational scope

The host LEAN launcher must be running with the configured lake mount; it was
started for this validation. The Python data service was restarted to load the
repair. Canonical preparation upgraded the tested legacy quote/metadata
artifacts. Failed historical results were preserved; successful runs are new
rows. No subscription, live broker feed, account, or trading control changed.
Database-destructive tests used an isolated disposable PostgreSQL container,
not the shared development catalog.
