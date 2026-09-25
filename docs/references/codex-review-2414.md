# Stocks data integrity, causality, and analytical claims

Research ticket: [[Codex] Stocks data integrity, causality, and analytical claims](https://github.com/tim1016/learn-ai/issues/2414). Baseline: `10b5f31b529c8507bd19bb24015f3d85fa9aba43`. This is an independent first pass, made without reading earlier maps, their children, audits, or the defect backlog. Findings below are recommendations, not fixes. No owner decision is reversed.

Five **High** findings were reproduced with synthetic data. Their reach differs: the lake defect reaches the historical-data authority; the export defect changes downloaded datasets; the two chart defects affect the Stocks analytical display, and have not been shown to change the execution engine; the return-study defect affects its displayed distribution and risk statistics. No Critical finding is claimed without an order, position, P&L, or validation-verdict reproduction.

## 1. Lake publication accepts structurally corrupt bars and can change their date

**Claim:** A successful vendor response can be published as a valid lake artifact without checking unique/increasing timestamps, membership in the requested trading date, OHLC consistency, or nonnegative volume. Encoding only the time of day can silently move a bar from a different date onto the requested date.

**Goal:** Correctness; architecture; downstream execution-data integrity.

**Severity:** High — an undetected corruption class in the shared source of historical bars. Actual bad vendor payloads and resulting trades were not inspected.

**Evidence — reproduced:** `test_lake_publishes_wrong_day_impossible_duplicate_bars` calls the actual artifact processor with an in-memory catalog claim and publication boundary. Two identical July 3 bars are accepted for a July 2 artifact, with open 100 / high 90 / low 110 / close 120 / volume −1. Publication is called and a success record is returned. Decoding the published bytes produces July 2 bars, retaining duplicates and impossible prices/volume.

- The [vendor decoder](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/polygon_fetcher.py#L164) casts fields but does not validate the stream.
- The [artifact processor](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/ensure_data.py#L661) sends converted bars directly to encoding and publication after checking only that the response is nonempty.
- The [writer](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/lean_writer.py#L108) removes the bar's date; the [reader](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/engine/data/lean_format.py#L147) reconstructs it from the artifact's date. Reader lines 160–161 also silently skip rows with the wrong column count.
- The [engine materialization bridge](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/run_materialization.py#L121) requests these trade artifacts for backtests. Its existence/size checks do not establish semantic bar validity.

**Why it matters:** A file hash proves which bytes were published, not whether their prices, time, and identity are valid. Downstream chart timestamp validation catches duplicate bars later, which is a useful safeguard, but it cannot recover the discarded original date of an otherwise monotonic wrong-day stream. An invalid artifact can also remain reusable in the catalog.

**Recommendation:** Establish one finite-ingestion validator before publication: exact timestamp type/unit, strict order/uniqueness, requested symbol/day membership, supported session/minute alignment, finite positive tradable prices, OHLC envelope, and volume domain. Reject malformed stored rows explicitly; expose a typed corrupt-artifact outcome. Validate semantic content separately from atomicity, leases, and hashing.

**Confidence:** High for acceptance and re-dating, proven at the production processor boundary. The publication step is mocked, so no real file/catalog publication or actual engine trade is claimed. A verified upstream validator on every path into this processor would change the reach; none appears in the traced path.

## 2. A one-day workspace selection exports the preceding session too

**Claim:** Stocks commits its date intent as UTC midnight, while dataset generation interprets that same start as an ET calendar date. The resulting export includes the previous trading date even though the chart uses the chosen date.

**Goal:** Correctness; trading intelligence; reproducible research scope.

**Severity:** High — ordinary UI requests produce the wrong dataset window without a rejection.

**Evidence — reproduced:** For `2024-07-02T00:00:00Z` through that day's final UTC millisecond, `prepare_generation_request` changes `from_date` to `2024-07-01`; `chart.resolve_request_dates` returns July 2 through July 2. Passing complete synthetic July 1/2 RTH sessions through the actual prepared export processor returns 780 rows, including 390 rows before the numeric start.

- The [frontend mapper](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lab/data-lab-request-mapper.ts#L155) sends both UTC-derived dates and the committed numeric bounds.
- The [generation resolver](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/dataset_plan_service.py#L327) takes the ET date of the numeric start and changes the fetch window.
- The [export processor](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/dataset.py#L185) processes that date range; it does not crop the result to `start_ms_utc`/`end_ms_utc`. Warmup trimming is based on the already-transformed `from_date`.

**Why it matters:** A chart and export from one committed workspace scope can describe different observations. Extra rows can change research samples or cross a downstream train/test boundary. The latter is a consequence if the exported file is consumed that way, not an observed backtest defect.

**Recommendation:** Define one meaning for a date-intent window and one resolver for chart, plan, generation, study, and companion files. If numeric windows represent actual half-open instants, enforce them on final rows after context/warmup reads; if they encode calendar-date intent, decode them consistently and separately from instant windows. Assert chart/export session membership on DST, ordinary, holiday, and early-close cases.

**Confidence:** High. Uses the exact frontend bounds and production request preparation/processing. The final reproduction disables the unrelated previous-close companion fetch and stubs only the aggregate fetch.

## 3. Stocks chart discards warmup before calculating indicators

**Claim:** The chart fetches historical warmup but computes indicators only after removing it. Changing the visible left boundary therefore changes calculated indicator values on overlapping timestamps.

**Goal:** Correctness; trading intelligence.

**Severity:** High — realistic, materially different indicator values in the active analysis surface.

**Evidence — reproduced:** With a complete prior session at price 100 and the displayed session at 200, EMA(5)'s first displayed point is null and its fifth is 200. The warmed recurrence at that fifth point is `200 − 100 × (2/3)^5 = 186.83127572016463`. The real chart entry point requests an earlier start but loses that context.

- [Warmup is requested](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/chart_service.py#L1190).
- [RTH preprocessing](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/chart_service.py#L539) only retains the visible-range schedule; [explicit trimming](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/chart_service.py#L1237) also precedes computation.
- [Indicators are computed afterward](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/chart_service.py#L1270).
- The [active Stocks chart](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lab/data-lab-chart/data-lab-chart.component.ts#L420) calls `/api/chart/data`.

**Why it matters:** A user can infer a crossover or regime difference from a chart boundary artifact. This is **chart-only evidence**: the [dataset pipeline](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/dataset_service.py#L811) calculates indicators before trimming. Neither exported indicator warmup nor execution-engine warmup is shown to share this defect.

**Recommendation:** Keep calculation context separate from display scope, calculate on correctly filtered and resampled warmup plus visible data, then trim the rendered result. Cache calculation windows with their required warmup and bar revisions. Verify overlapping-window indicator invariance with independently calculated recurrences.

**Confidence:** High for the Stocks route. Reproduction stubs data sourcing only and uses an independent EMA recurrence.

## 4. RTH four-hour chart bars are anchored one hour before market open

**Claim:** The chart's shared 30-minute offset works for hourly bars but does not anchor four-hour bars to the 09:30 ET session open.

**Goal:** Correctness; trading intelligence.

**Severity:** High — actual OHLC grouping and indicator inputs differ from the advertised session-anchored bar definition.

**Evidence — reproduced:** A complete 390-minute RTH session becomes bars labelled 08:30 and 12:30 ET with volume totals 180 and 210. A four-hour grid anchored at session open is 09:30/13:30, containing 240/150 minutes. The [resampler applies a 30-minute offset to all intraday timeframes](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/chart_service.py#L703), and [calls pandas resample with that offset](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/chart_service.py#L736). The function's documented contract at line 685 explicitly says RTH intraday is anchored to 09:30.

**Why it matters:** This changes candle membership, not just labels: an extreme or close between 12:30 and 13:30 belongs to a different candle, altering chart indicators. Execution-engine consolidation was not exercised; do not infer a bot defect from this chart result.

**Recommendation:** Bucket each session against its actual calendar open, enforce its close, and represent shortened final bars explicitly. Share the semantic timeframe policy with execution/replay consumers, or disclose different policies. Verify both membership and timestamps for four-hour bars and early closes.

**Confidence:** High. Direct production resampler, complete synthetic minute data, exact integer volume oracle.

## 5. Return Distribution treats arbitrary partial-session data as daily anchors

**Claim:** Any first/last observed RTH minute becomes the session open/close. A capture missing both session boundaries can silently produce ordinary full-session, close-to-close, and overnight statistics.

**Goal:** Correctness; trading intelligence; missing-data honesty.

**Severity:** High — sample and tail statistics can be numerically correct for the wrong observations with no partial-session indication.

**Evidence — reproduced:** A complete synthetic source has a session open of 90, one noon minute open 100 / close 101, and a session close of 110 on every scheduled date in May–June 2024: its full-session return is +22.222222…%. Removing the open/close observations from the captured files leaves exactly the noon minute. The real study reader and computation still report zero missing sessions, zero excluded sessions, and a session mean of +1%; warnings contain no missing-minute/partial-anchor notice. The complete fixture yields +22.222222…% through the same production reader. Identity factors are stubbed solely to remove an unrelated missing-factor warning.

- [Anchor extraction](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/return_distribution.py#L298) picks the first and last bar anywhere inside RTH without recording their distance from the named boundaries.
- [Coverage](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/return_distribution_service.py#L293) excludes a day only if it has no usable RTH anchors and counts missing files, not missing boundary observations.
- The [frontend adapter](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lab/returns-distribution/returns-distribution.service.ts#L135) faithfully presents those Python coverage/statistics fields; there is no additional anchor-quality field in this response.

**Why it matters:** Missing data and illiquidity are different explanations for the same sparse file. Neither permits silently describing a noon minute as the day's full-session move. Excluding whole missing dates already protects scheduled adjacency, but does not protect against partial endpoints within an existing file.

**Recommendation:** Record the actual timestamp and provenance of every anchor and its completeness/freshness relative to the intended boundary. Mark unavailable segment returns null, explain exclusions per return kind, and distinguish sparse-but-valid last-trade semantics from incomplete capture. Apply the same rule to study and drilldown receipts.

**Confidence:** High for the observed behavior; deciding the permitted stale-last-trade tolerance is a product/domain choice. The recommendation does not demand a fabricated trade every minute.

## Coverage and satisfied controls

The [Stocks menu](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shell/app-menu.ts#L43) and [child routes](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lab/data-lab.routes.ts#L29) were followed breadth-first:

| Surface | Traced authority and contract | Result/limit |
|---|---|---|
| Explore chart/indicators | Angular chart → FastAPI `chart.py` → `chart_service`, `chart_bar_source`, materialization → lake/provider; `schemas/chart.py`, committed OpenAPI | Active chart defects above. Source receipts, typed provider failure, explicit synthetic-bar flags exist. |
| Export/plan | Mapper/plan service/run session → .NET `JobsApi` dataset-zip dispatch → Python jobs/dataset router → plan/dataset services | Wrong numeric-window interpretation above. Indicator-before-trim ordering is correct in export; generated-column selection is checked before fetch. |
| Validate | Angular validate → data-quality analyze and CSV validation-report → `data_quality_service`, `validation_service` | Read directly; exploratory quality processing is not evidence that lake ingestion is validated. CSV positional fallback and pair-only grades warrant a focused followup before treating reports as release gates. |
| Return Distribution/drilldown | Direct FastAPI → return distribution service → raw lake/factors → pure statistics; generated DTOs | Missing whole sessions break close-to-close adjacency; absent factor files produce an explicit raw warning; bins and drilldown membership share one Python classifier. Partial boundary issue above. |
| Lake Observatory/backfill | Angular lake service → Python coverage/artifact/storage/default/backfill routes → catalog/ensure-data; .NET job dispatch | Coverage explicitly selects adjustment mode, provider, and root identity and enumerates canonical scheduled sessions, including missing rows. Semantic publication gap above. |
| Saved workspace/setup | `data-lab-session.service.ts` → .NET `DataLabQuery`/`DataLabMutation` → persistence; GraphQL snapshot | .NET transports workspace/opaque snapshot state; does not calculate the chart/study math. Legacy DateTime/date strings coexist with authoritative numeric fields as an explicit migration. No runtime database accessed. |
| Older aggregate GraphQL path | `Query.GetOrFetchStockAggregates` → `MarketDataService` → Python aggregate/sanitizer | Read for backend coverage. Cache key ignores adjustment and gap logic uses weekdays, but this is not the current Stocks chart path, so those are not charged here as active menu defects. |

Positive verification: **39 existing tests passed**, including RD-001 production-vs-independent golden oracle, return moments/bin membership and scheduled adjacency, factor lookup parity, strict duplicate/out-of-order chart rejection, and deterministic lake encoding. These controls are valuable; they do not test the five reproduced failures.

## Reproduction and safety receipt

Files: [synthetic characterizations](reproductions/codex_review_2414.py), [guarded launcher](reproductions/codex_review_guard.py). Tests assert the observed baseline behavior. They are throwaway research assets, not production regression tests or patches. All temporary lake files are created and removed under this isolated clone. No containers, live/paper state, shared Postgres, real lake, credential files, or prior audits were accessed. Only the host virtual environment was read from the main checkout.

From `/Users/inkant/codex-review-20260924/stocks/PythonDataService`:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=/Users/inkant/codex-review-20260924/stocks PYTHONDONTWRITEBYTECODE=1 POLYGON_API_KEY=synthetic-review-key DATA_PLANE_CONTROL_SECRET='' TMPDIR=/Users/inkant/codex-review-20260924/stocks/.review-tmp /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B ../docs/references/reproductions/codex_review_guard.py ../docs/references/reproductions/codex_review_2414.py
```

Outcome: **5 characterizations passed**. The temporary directory must exist. The launcher restricts execution to the review tree, blocks sockets/DNS and unguarded subprocesses, denies protected-file reads, and denies writes outside the review tree. One intermediate reproduction accidentally retained the previous-close companion default; the launcher blocked its attempted DNS resolution before any provider connection. The final fixture explicitly disables that unrelated fetch. Two earlier fixture-constructor errors were corrected before the final run.

Existing-test command uses the same environment/launcher with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, followed by:

```sh
-m pytest --noconftest -o addopts='' --basetemp=/Users/inkant/codex-review-20260924/stocks/.review-tmp/pytest tests/research/test_return_distribution.py tests/fixtures/test_return_distribution_golden.py tests/data_lake/test_factor_files.py tests/services/test_chart_preprocess_temporal.py tests/unit/data_lake/test_lean_writer.py -q
```

Outcome: **39 passed**. `--noconftest` deliberately avoids unrelated root fixtures that import broker runtime; all selected tests are self-contained synthetic/local-fixture tests. Warnings: deprecated Polygon SDK `pkg_resources`, and unused `asyncio_mode` pytest setting because automatic plugins were disabled. No service startup occurred. Full `ruff check --no-cache PythonDataService/app/ PythonDataService/tests/` and the review reproduction files passed; no production file was changed.

## Precise followups

1. Does a return study's factor file cover the complete requested price history and corporate-action horizon, or does the mere existence of any nonempty factor file make stale/narrow captures read as fully adjusted? Investigate `_probe_lake_coverage`, factor rebuild windows, ticker rename/map semantics, and immutable identity lineage together.
2. Can a CSV validation report grant an excellent overall grade with unmatched timestamps, duplicate timestamp Cartesian joins, mismatched warmup regions, or positional alignment? Determine whether it is advisory comparison or an admissible validation verdict.
3. Does backtest/live consolidation use the same session-open/timeframe and anchor policy as Stocks? The two chart findings do not establish execution-engine impact.

Potential owner question, only after policy evidence is gathered: for an illiquid instrument without a print at a session boundary, should Return Distribution present a labelled last-observation return with explicit anchor ages, or omit that segment? Recommended default: omit an unproven boundary and expose the reason; never silently relabel it. None of this first-pass ticket's resolutions requires an owner answer.
