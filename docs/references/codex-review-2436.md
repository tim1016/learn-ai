# Instrument coverage selection and capture lifecycle

Research ticket: [[Codex] Instrument coverage selection and capture lifecycle](https://github.com/tim1016/learn-ai/issues/2436). Source baseline: [`10b5f31b529c8507bd19bb24015f3d85fa9aba43`](https://github.com/tim1016/learn-ai/tree/10b5f31b529c8507bd19bb24015f3d85fa9aba43). Research branch: `research/codex-2436-coverage`.

**Result: one new Medium finding; no new Critical or High.** The unheld-pick gate preserves symbol, adjustment mode, session ownership and current admission in the examined paths. A separate Observatory reattachment defect drops earlier capture receipts and failures from the panel. This review does not repeat bar validation, corporate-action lineage, adjustment vintage, or publication/read findings.

## Authority and scope

ADR 0066 explains why a lake-only menu was too restrictive: “A newly listed symbol was unpickable everywhere until an operator went to the Data Lake Observatory”. Its decision is “Populate, then use”, with a fresh lake read, and its architecture keeps “one backfill state machine, not two.” This review accepts those reasons and recommends satisfying the existing decision. The listing catalog is a current membership convenience, not a historical universe or survivorship guarantee. [ADR 0066, context and decisions](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0066-symbol-picker-offers-the-listing-universe-and-populates-the-lake.md#L18).

The implemented gate asks for the provider-allowed history, then establishes **membership in the requested adjustment tree**, not complete coverage of every date in a host's eventual run window. It deliberately admits partial captures when trade bars exist for that symbol; a held MIN/MAX span explicitly does not promise intervening sessions. This is not counted as a newly discovered failure of selection identity. [Window composition](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/symbol-catalog/ensure-coverage.service.ts#L324), [partial-capture admission](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/symbol-catalog/ensure-coverage.service.ts#L390), [span semantics](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/ticker-catalog/ticker-catalog.service.ts#L125).

Examined the Stocks menu entry points, single and multi instrument cards, picker wrappers, joined listing/lake membership, per-mode lake views, `CoverageGateController`, `EnsureCoverageService`, shared backfill runner, root jobs registry, Observatory panel/store/template, .NET job dispatch/SSE, Python backfill defaults/request validation, worker cancellation and per-day outcome contract. Relevant Angular/testing rules were read. [In-scope menu](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shell/app-menu.ts#L39).

## C1 — Same-tab capture reattachment loses earlier receipts and failures

**Claim:** Navigate away from an Observatory capture after some session events, then return while that job is still running. The new panel subscribes only to future frames on the existing root stream. Earlier session rows, progress and failures are not reconstructed, despite the reattachment message. Completion can therefore show only the successful tail of a partially failed capture.

**Goal:** Correctness of capture evidence; architecture with one authoritative job projection.

**Severity:** **Medium.** This is bounded, recoverable degradation of operator evidence. The probe does not show altered lake bytes, a wrong order or validation result, or bypass of the picker's separate fresh-coverage check. Coverage can be checked independently, and the omitted day indices can reveal incompleteness.

**Evidence — proven from source:** The panel automatically adopts an active backfill on construction; its panel-scoped store resets receipt rows and invokes `runner.observe(jobId)`. That runner creates empty lifecycle state and registers a listener. The root `JobsService.onEvent` explicitly does **not** reopen a stream or replay earlier frames; it only adds a callback. The .NET endpoint's replay occurs when an SSE connection is opened, which does not happen on this same-tab reattachment. The template nevertheless says: “Everything below was replayed from the job's own event stream”. Sources: [panel adoption](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lake-observatory/backfill-panel/lake-backfill-panel.component.ts#L230), [store reset and reattach](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lake-observatory/lib/data-lake-backfill.store.ts#L133), [runner observation](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/data-lake/backfill-job-runner.ts#L110), [root subscription](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/services/jobs.service.ts#L185), [server replay](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Backend/Jobs/JobsApi.cs#L181), [rendered claim and failure list](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lake-observatory/backfill-panel/backfill-run-log.component.html#L7).

**Evidence — reproduced:** The offline harness executes the actual TypeScript `JobsService`, `BackfillJobRunner`, and `DataLakeBackfillStore` with explicit signal/DI/HTTP/EventSource doubles. A three-session stream has a nonfatal `provider_api_error` on day 1; the original panel observes it. Destroying that panel, emitting day 2 while absent, and reattaching a new store opens no second transport. Day 3 and terminal completion leave `reattached=true`, `phase=completed`, `dayIndexes=[3]`, and `failuresVisible=0`. The root retained one stream throughout. The chosen provider error is not a globally fatal worker reason, so the later sessions are reachable. [Executable reproduction and controls](https://github.com/tim1016/learn-ai/blob/0f230e08d0c44006d1234d2c692af2cfd457f26e/review/codex_2436_coverage.cjs#L224), [worker fatal-reason policy](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/backfill.py#L74).

**Counter-evidence:** When historical frames actually are delivered, the store reconstructs all three rows, keeps the earlier failure and deduplicates a repeated day. The server already supports replay. The defect is the late subscriber's missing history, not the per-day fold or the server's replay implementation. The terminal event still identifies completion of the job, not proof that every day succeeded.

**Why it matters:** Navigating within the app should not erase the capture failure that explains a gap. The operator can otherwise inspect a completed capture and see only later successful days beneath a replay assurance.

**Recommendation:** Keep domain receipt history or its typed projection under the shared job owner, and give a late panel subscriber an atomic snapshot plus subsequent events. Alternatively, use a coordinated replay/cursor contract that preserves the single shared lifecycle. Explicitly mark incomplete history if retention or replay cannot satisfy it. Add a rendered navigation-away/return regression that checks earlier failures remain visible. This satisfies ADR 0066's shared-runner rationale; no reversal is needed.

**Confidence:** High in the source-level failure. The source-execution probe is not a browser/Angular integration test: it does not emulate Angular scheduling or render the template. An unseen replay source called by this reattach path would change the conclusion; none was found in the traced path.

## Satisfied controls and limits

- **Identity and ownership — reproduced.** `{symbol, mode}` keys separate unrelated captures. Identical asks share a job; releasing one caller does not cancel its co-waiter. Last-caller cancellation resolves that caller false before server cancellation, and late defaults/completion cannot revive the old gate. Cancellation during submission cancels the eventually accepted job. [Run ownership](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/symbol-catalog/ensure-coverage.service.ts#L161), [release and liveness](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/symbol-catalog/ensure-coverage.service.ts#L206).
- **Admission — reproduced.** A job completion alone, a cached pool update, or another symbol's fresh span cannot admit the pick. The gate reads fresh `usa`/requested-mode/`trade` coverage and checks the same symbol. Unknown coverage refuses; empty completion retains the latest typed root failure. A positive partial span is intentionally membership. [Fresh admission](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/symbol-catalog/ensure-coverage.service.ts#L403).
- **Current selection — mixed reproduced/static.** The actual controller rejects superseded sessions and rechecks the current mode before commit; retry rechecks lake availability and unsupported modes refuse. Source inspection shows single-card symbol/mode/destroy invalidation and multi-card selection-reference checks. Those Angular effects and rendered interactions were inspected, not integration-tested. [Controller](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/symbol-catalog/coverage-gate.controller.ts#L101), [single-card lifecycle](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/ticker-range-picker/parts/instrument-card.component.ts#L153), [multi-card admission](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/shared/multi-ticker-range-picker/multi-instrument-card.component.ts#L124).
- **Request and worker boundary — tested/static.** All weekday window compositions in the harness fit the cap and provider floor. Python route tests reject malformed symbols, invalid timestamp encodings, reversed/over-cap windows and pre-provider-history starts. Worker tests exercise canonical sessions, absent expected artifacts, typed failures, lease waits, global-failure aborts and cooperative cancellation. .NET was inspected for job-ID injection and unchanged spec transport; no Redis or .NET integration environment was started. [Dispatch](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Backend/Jobs/JobsApi.cs#L102), [Python admission](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/data_lake.py#L505), [worker contract](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/backfill.py#L482).

## Safe verification

**15 TypeScript source-execution checks passed**, including the reproduced C1 defect and its history-present control. **50 existing Python tests passed**: 30 worker, 15 route, 5 defaults. Existing Angular component specs were inspected but not run. No dependency installation or symlink, browser, service, container, database, vendor request, live/paper state, or protected file was used. Only the compiler module was read from host frontend dependencies; app code came from the isolated baseline clone. Node's permission mode disallowed network/children/writes. Python used the separate review guard against network, forbidden file reads and writes outside the review tree.

From the isolated clone:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  HOME=/Users/inkant/codex-review-20260924/stocks \
  /opt/homebrew/bin/node --permission \
  --allow-fs-read=/Users/inkant/codex-review-20260924/stocks \
  --allow-fs-read=/Users/inkant/learn-ai/Frontend/node_modules/typescript \
  review/codex_2436_coverage.cjs
```

From the isolated clone's `PythonDataService` directory (the review launcher is session infrastructure, not a repository dependency):

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  HOME=/Users/inkant/codex-review-20260924/stocks \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  POLYGON_API_KEY=synthetic-review-key DATA_PLANE_CONTROL_SECRET='' \
  TMPDIR=/Users/inkant/codex-review-20260924/stocks/.review-tmp \
  /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B \
  /Users/inkant/codex-review-20260924/control/guarded_run.py \
  -m pytest --noconftest -p pytest_asyncio.plugin -o addopts='' \
  --basetemp=/Users/inkant/codex-review-20260924/stocks/.review-tmp/2436 \
  tests/unit/data_lake/test_backfill.py \
  tests/routers/test_data_lake_backfill_job.py \
  tests/routers/test_data_lake_backfill_defaults.py
```

No new independent research frontier or owner decision is required by this bounded result. A future implementation should validate C1 in the real Angular router/runtime and confirm late subscribers receive complete receipts before presenting replay assurance. That is validation of the recommendation, not evidence of an additional finding.
