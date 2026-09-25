# Historical instrument, adjustment, and materialization lineage

Research ticket: [#2420](https://github.com/tim1016/learn-ai/issues/2420). Baseline: `10b5f31b529c8507bd19bb24015f3d85fa9aba43`. Date: 2026-09-24. Report-only branch: `research/codex-2420-lineage`.

This bounded followup traced the shared listing picker, historical corporate-action inputs, mode-specific lake artifacts, and Python/LEAN materialization. It found three new **High** issues. These concern adjustment/identity lineage and do not recount the earlier Stocks ingestion, date-boundary, indicator-warmup, resampling, or incomplete-session-anchor findings. Severity describes impact when the stated trigger occurs; it does not assert that any existing real dataset is affected.

## Findings

### L1 — A bar-complete study can present an uncovered corporate-action window as fully adjusted

**Claim:** The return study treats any nonempty factor file as proof of split/dividend adjustment, even if its captured horizon ends before an in-study corporate action. No missing bar means no materialization attempt, so the factor-file refresh logic never runs.

**Goal:** Correctness; intelligence.

**Severity:** **High.** A mechanically neutral split becomes a large price loss in the distribution, tail statistics, and per-day result while the response explicitly claims adjustment and carries no warning.

**Evidence — reproduced and proven:** The [coverage probe checks only session zip presence](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/return_distribution_service.py#L203), and [capture runs only when that probe requires it](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/return_distribution_service.py#L390). [Any factor rows select `split_and_dividend`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/return_distribution_service.py#L277). The [builder includes only events within its requested horizon](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/factor_files.py#L84), and the [reader uses identity after the last factor row](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/factor_files.py#L236).

The synthetic reproduction seeds **every RTH minute**, with exact session-open/close anchors, for the lead-in and May 1–June 28, 2024. A synthetic 2:1 split on June 3 changes raw price 100 to 50 without economic movement. A production-built factor file ending May 31 gives June 3 close-to-close return **−50%**, `adjustment=split_and_dividend`, zero missing/excluded sessions, no warnings, and `capture=not_attempted`. Replacing only the factors with a production-built file through June 28 gives **0%**. Absolute tolerance is `1e-12` percentage points, relative tolerance zero. Capture is stubbed and asserted never called. The router [passes adjustment and warnings directly into the response](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/return_distribution.py#L121).

**Why:** The window-sensitive [factor contract and refresh-on-mismatch](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/ensure_data.py#L724) protect callers that actually request factor materialization. They do not protect a study that short-circuits on bar coverage. Moreover, one fixed factor-file path is replaced for either wider or narrower requested windows; it is not a monotonic, whole-symbol history. A previously captured narrow study plus chart-captured wider bars is sufficient to reach the defect. A failed refresh may retain the prior file; presence alone cannot establish the claimed adjustment scope.

**Recommendation:** Give factors explicit coverage and corporate-action revision provenance, validate it for the full study read window, and include that requirement in the materialization probe. Prefer a versioned or monotonically complete action history over replacing a global symbol file with arbitrary request windows. If factor coverage cannot be established, withhold the adjusted claim and either refuse or report a clearly scoped raw/incomplete result.

**Confidence:** High. The public service orchestration and numerical result are reproduced. No claim is made about current production factor coverage.

### L2 — Map files encode a capture-window end as a permanent historical boundary

**Claim:** The first map capture fixes a start/end range that later, wider requests reuse unchanged. The LEAN admission path includes the existing map without validating that horizon. In LEAN, these dates are security-history bounds, including a delisting boundary; they are not merely receipt timestamps.

**Goal:** Correctness; architecture.

**Severity:** **High.** A later LEAN research/backtest request can have all bar artifacts present yet carry a map that excludes part of its requested history or implies a synthetic delisting. This compromises the claimed run window and cross-engine comparison.

**Evidence — reproduced local cache behavior; engine effect inferred from pinned primary source:** The [map builder writes request start and end](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/map_files.py#L36). Its [contract hash omits both dates](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/ensure_data.py#L246), and [any existing complete map is returned as a cache hit](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/ensure_data.py#L947). The reproduction requests through June 28 with an existing May 1–31 map: it is reused, no event fetch occurs, the final row remains `20240531,synth,nyse`, and the sidecar's actual corporate-action-file selector exposes it.

The [sidecar validates bar/daily/metadata coverage but only checks corporate-action file existence](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/lean_sidecar/lake_mount.py#L410); [the active service calls that resolver before launch](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L560). The repo [pins LEAN source commit `261366a7…`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/lean_sidecar/config.py#L103). At that exact commit, [LEAN `MapFile` sets `DelistingDate` from the last row and `HasData` refuses dates outside its first/last rows](https://github.com/QuantConnect/Lean/blob/261366a7e26ae942df858ab20df4fef8fa07de67/Common/Data/Auxiliary/MapFile.cs#L70). No container or LEAN backtest was executed in this review; the final runtime consequence is source-derived, not measured here.

**Why:** Corporate-action identity metadata is being constructed from a particular capture request and then cached as whole-symbol truth. This is independent of the explicitly deferred reconstruction of historical ticker changes: the reproduction uses a symbol that never changes ticker. It also applies to expanding the start date earlier than the first capture. Hashing the existing map in a run manifest preserves the wrong boundary; it does not validate the boundary.

**Recommendation:** Separate security identity and true listing/delisting dates from capture coverage. Update maps when their authoritative history changes and reject maps that cannot serve the requested window. At minimum, do not cache request-window endpoints under a window-independent contract or interpret a capture end as a delisting date. Exercise both earlier-start and later-end expansion with the pinned LEAN semantics.

**Confidence:** High on cache/producer/admission defect; high source-based confidence in LEAN boundary meaning. Exact downstream trades or truncation diagnostics were not executed.

### L3 — Split-adjusted bar artifacts can mix incompatible capture vintages

**Claim:** A completed `polygon_split_adjusted` day is reused without a corporate-action revision/as-of check. Later days can therefore be captured on a newer share basis while earlier cached days remain on the older basis, all under the same adjustment mode and contract.

**Goal:** Correctness; intelligence; architecture.

**Severity:** **High.** An adjusted chart or adjusted Python backtest spanning a newly occurred split can consume a discontinuity that should have been removed. Receipt hashes can faithfully identify these bytes while leaving the series economically inconsistent.

**Evidence — reproduced cache/read behavior; vendor-revision premise from primary documentation:** The [minute contract includes the `adjusted` flag but no corporate-action version](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/ensure_data.py#L210). The [complete-day cache path returns the existing record without fetching](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/ensure_data.py#L482). [Chart reads select the adjusted lake root](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/chart_bar_source.py#L405); [Python backtest materialization passes through the requested adjustment mode](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/engine_backtest_service.py#L425).

The reproduction supplies a completed adjusted day captured before a synthetic 2:1 split at price 100, then a post-split day at 50. The actual minute processor reuses the old record without calling the provider, and the actual lake reader returns 100 then 50. A common post-split basis would give 50 then 50. This is a cache-admission characterization, not a vendor simulation asserting any specific real ticker's prices. The provider [documents that adjusted historical aggregates change retroactively with subsequent splits, and recommends storing raw prices plus split records for a permanent record](https://massive.com/knowledge-base/article/is-massives-stock-data-adjusted-for-splits-or-dividends). No market-data API was called.

**Why:** Separating raw and adjusted roots prevents cross-mode contamination, but an adjustment flag alone does not identify a stable historical price basis. Full completed-session coverage and matching file sizes do not detect a mixture of valid artifacts downloaded on different bases. The separate factor file does not solve this path: the [Python engine spec deliberately requests only the trade bars it reads](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/run_materialization.py#L118).

**Recommendation:** Make raw prices plus versioned action history the stable historical authority and derive a consistent adjusted view for a declared revision/as-of policy, or explicitly invalidate/rebuild all affected adjusted history when the provider's basis changes. Bind that basis to artifact and run receipts; ordinary reuse must prove consistency across the window.

**Confidence:** High for unchanged reuse and reader output; high for the provider's stated semantics. No affected real capture or trading loss is alleged.

## Controls, coverage, and decisions respected

| Surface | Checked result |
| --- | --- |
| Listing catalog and selection | [Provider walk](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/polygon_client.py#L1063) requests current active/inactive listings, deduplicates by symbol, and projects display membership without a point-in-time identity. [Shared picker](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/symbol-catalog/symbol-catalog.service.ts#L64) offers active vendor names plus held symbols and visibly handles degraded catalog reads. It must not be interpreted as a survivorship-free historical universe, but no such guarantee was found here. |
| Delisted membership policy | [ADR 0066](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0066-symbol-picker-offers-the-listing-universe-and-populates-the-lake.md#L69) deliberately makes vendor-only delisted names opt-in: “an operator's visible choice rather than a default that accretes by accident.” This review accepts that reason; the active-only default is not counted as a defect. |
| Catalog availability | [One-hour cache](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/ticker_catalog_service.py#L21), singleflight, caller-cancellation shielding, and failed-task eviction are present. A catalog cache TTL is not corporate-action coverage evidence. |
| Ticker identity/maps | Map builder openly defers ticker-history reconstruction and emits the current symbol throughout; production also supplies `nyse` as the exchange. These limitations were inspected. No separate rename/reused-ticker finding is counted without an end-to-end identity oracle. L2 does not depend on either limitation. |
| Adjustment/root isolation | Artifact identity includes [physical root and adjustment mode](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/types.py#L290); [catalog predicates](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/catalog_client.py#L223) scope them. No database-backed validation was performed. L3 concerns distinct vintages within one correct mode/root. |
| Python partial policy | [Resolution-aware coverage gate](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/run_materialization.py#L285) refuses missing minute sessions and any missing source/aggregate bars for a daily run. Irrelevant metadata/daily failures may pass with an explicit diagnostic. Existing offline tests passed. |
| File/receipt integrity | [Admission checks existence and size](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/run_materialization.py#L338). The code explicitly chooses a cheap stat over per-run full hashing. Its [availability fingerprint is documented as materialized lake state, a superset of consumed artifacts](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/run_materialization.py#L445); the Python service separately [binds minute zip hashes to the data-policy fixture receipt](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/engine_backtest_service.py#L386). No claim of universal byte-for-byte gate verification is made. Concurrent mutation between receipt/read was not stress-tested. |
| LEAN admission | Required session trade/quote, daily span, and metadata checks are explicit. Existing factor/map paths enter [manifest hashes](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L1110). L2 identifies a semantic coverage check those hashes cannot provide. |

The findings do not reject ADR 0049's file authority, mode separation, or in-process orchestration. Its [partial-policy decision](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md#L90) explains that `ensure_data` cannot choose a universal policy because callers differ between strict backtests and best-effort exploration. That reason remains valid; the demonstrated failures concern insufficient evidence for the semantics callers advertise, not the location of orchestration. Its supported catalog-mediated replacements also remain useful; L3 requires a policy that actually triggers consistent revisions.

## Reproduction and safety record

Artifacts: [characterizations](reproductions/codex_review_2420.py), [audit guard](reproductions/codex_review_2420_guard.py). Assertions deliberately characterize baseline defects, so passing means the stated behavior was observed; these are not regression tests for a fix.

Ran from the isolated clone's `PythonDataService` directory:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  HOME=/Users/inkant/codex-review-20260924/stocks \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  POLYGON_API_KEY=synthetic-review-key DATA_PLANE_CONTROL_SECRET='' \
  TMPDIR=/Users/inkant/codex-review-20260924/stocks/.review-tmp \
  /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B \
  ../docs/references/reproductions/codex_review_2420_guard.py \
  -m pytest --noconftest -p pytest_asyncio.plugin -o addopts='' \
  --basetemp=/Users/inkant/codex-review-20260924/stocks/.review-tmp/2420 \
  ../docs/references/reproductions/codex_review_2420.py \
  tests/unit/data_lake/test_factor_files.py tests/data_lake/test_factor_files.py \
  tests/unit/data_lake/test_map_files.py tests/unit/data_lake/test_run_materialization.py
```

Result: **65 passed** (3 new characterizations, 62 existing tests) in 2.13 seconds. One existing `RuntimeWarning` occurs in `test_materialize_run_data_sync_refuses_to_block_an_event_loop`: `_materialize_run_data` coroutine was never awaited. No extra finding is inferred from that warning. Ruff checked the two reproduction files plus all `PythonDataService/app/` and `PythonDataService/tests/`: **all checks passed**.

Imports and fixtures were inspected before execution. Root conftest/plugin autoload were bypassed. Catalog, provider, and launcher test interactions are in-memory/mocked; the guard rejects socket connects/DNS/binds, subprocess launch, `.env`/live-run reads, main-checkout reads outside the host venv, and writes outside the isolated review tree. The venv interpreter/libraries were read-only. All market prices, events, metadata fixtures, and catalog records were synthetic. No containers, live broker/vendor data, runtime databases, clerk state, or existing lake files were accessed. Only GitHub publication and public primary source documentation used the network. No production files changed, and no previous audit/defect archive was consulted.

## Sharply bounded followups and owner questions

1. **Historical identity:** Does a stable security identifier survive a ticker rename, symbol reuse, delisting, and exchange change across catalog → bars → map → engine? Use synthetic distinct entities sharing a ticker and a pinned reference oracle. This review establishes the absence of such catalog fields and deferred map handling, not a proven live identity collision.
2. **Receipt versus consumed bytes:** Under a controlled concurrent factor/day refresh, do Python and LEAN run manifests bind the bytes actually opened for the entire run? This is separate from the demonstrated wrong adjustment basis and should use only temporary files and fake catalog leases.
3. **Owner decision, if historical universe research is intended:** Should historical symbol selection promise a point-in-time survivorship-free universe, and at what as-of timestamp? Current ADR 0066 intentionally promises listing convenience plus lake coverage. An expanded research-universe guarantee needs an explicit scope and identity data contract.
4. **Owner decision for adjusted studies:** Choose and expose a consistent adjustment as-of policy (latest common action revision versus historically available revision) rather than allowing capture order to choose it implicitly. This affects the remedy for L1/L3, not whether their inconsistent current claims are acceptable.

No owner answer is required to establish these findings. Implementation, existing-data diagnosis, and new guarantees remain outside this research-only ticket.
