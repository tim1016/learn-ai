# Asynchronous study execution and durable result identity

Research: [#2430](https://github.com/tim1016/learn-ai/issues/2430), under [map #2413](https://github.com/tim1016/learn-ai/issues/2413). Reviewed 2026-09-24 at [`10b5f31b529c8507bd19bb24015f3d85fa9aba43`](https://github.com/tim1016/learn-ai/tree/10b5f31b529c8507bd19bb24015f3d85fa9aba43). Implementation links below pin that baseline.

## Result

**No new Critical or High finding. Two Medium findings affect cancellation and persistence reporting.** Neither reproduction changes a numerical result, an alpha verdict, an order, or a position. Both have bounded operational consequences: unwanted continued computation, and an unnecessary rerun when a save's outcome is still unknown.

Grid and Walk-Forward preserve saved configuration on Finish, bind cell reads to recorded data bytes, and fence writes by attempt generation. Strategy Lab's persistence deliberately remains best effort. This review recommends making that existing boundary observable, not reversing it.

## Scope and method

Applied the `research` skill as the delegated researcher, claimed the issue, reloaded the current map Notes, and started `research/codex-2430-jobs` from the fixed baseline in the isolated validation clone. Inspected the reachable Strategy Lab, Grid Search and Walk-Forward frontend services, .NET job facade, Python job/worker, durable records, contracts and recovery paths. Shared files also contain Research-only jobs; those product paths were not investigated. Previously reported numerical, selection and loaded-source qualification defects are not recounted.

No services, containers or infrastructure were started. No shared Postgres, Redis, broker/vendor endpoints, market-data fetches, live/paper state, `.env`, clerk volumes or live-run artifacts were accessed. Prior maps/audit reports/defect backlog were not used for discovery. Reproductions run behind the review audit hook with all network and subprocess creation blocked, clean environment variables, the read-only host interpreter, synthetic inputs, and in-memory boundary doubles.

| Surface | Execution and result ownership traced |
| --- | --- |
| Strategy Lab Python | `strategy-lab-runner.service.ts` → `JobsService` → .NET `JobsApi` → `jobs.start_engine_backtest_job` → engine service → backtest-run writer → result and saved report |
| Strategy Lab LEAN | Same facade → `start_lean_engine_run_job` → sidecar service boundary → persisted run/result; launcher was stubbed, never invoked |
| Grid Search | Frontend search service → facade → `routers/grid_search.py` → saved request/receipt → generation-fenced cells and final record |
| Walk-Forward | Frontend study service → facade → `routers/walk_forward_study.py` → saved study and owned fold sweeps → generation-fenced progress/verdict |

## J1 — LEAN's Cancel control never consults the cancellation flag

**Claim.** A direct Strategy Lab LEAN job ignores cooperative cancellation, including a flag already set before its work starts, and can run through to ordinary completion.

**Goal.** Accepted cancellation requests must have a defined, observable boundary: stop before execution when possible, or explain that an already-dispatched phase cannot be interrupted.

**Severity. Medium.** The consequence is continued unwanted computation and misleading control behavior. A cancellation request is not a guarantee that already-produced work is discarded; this finding does not assert such a guarantee, nor any effect on trading or numerical correctness.

**Evidence — proven.** The shared job control offers Cancel for both queued and running jobs ([`job-progress.component.ts:97-105,144-146,172-174`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/jobs/job-progress.component.ts#L97-L174)). .NET acknowledges the request by storing `cancel_requested=1`, leaving acknowledgement of actual cancellation to the worker ([`JobsApi.cs:320-329`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Backend/Jobs/JobsApi.cs#L320-L329)). LEAN calls `cancel.raise_if_cancelled()` once, before entering its orchestrator, and then returns its result without another check ([`routers/jobs.py:722-756`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/jobs.py#L722-L756)). Its launch omits the polling override ([`:787`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/jobs.py#L787)); the runner defaults to 1,000 ([`jobs/runner.py:44-67`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/jobs/runner.py#L44-L67)). The first 999 calls answer false without reading Redis ([`jobs/progress.py:222-243`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/jobs/progress.py#L222-L243)). With only one call, this job never reads the flag at all.

**Evidence — reproduced.** [Synthetic tests](https://github.com/tim1016/learn-ai/blob/research/codex-2430-jobs/review/test_codex_2430_jobs.py), `test_lean_preexisting_cancel_is_ignored_but_engine_control_honors_it`, captures the real route's worker and runner options, sets cancellation in the in-memory Redis double, and runs the real thread runner and cancellation class. The stubbed LEAN orchestrator is called and the final state/event are `completed` / `job.completed`. The real Python-engine sibling under the same setup finishes `cancelled`, because it explicitly polls every call. This proves the boundary behavior without launching LEAN or contacting Redis.

**Why it matters.** The user cannot stop the LEAN job through its advertised control, even at the code's intended pre-execution checkpoint. A request made after dispatch also has no later polling site. This wastes compute and makes the cancellation state untrustworthy, but it does not establish a corrupt result.

**Rationale and recommendation.** The shared framework deliberately uses cooperative polling to avoid an expensive store read per inner-loop iteration ([`progress.py:202-225`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/jobs/progress.py#L202-L225)). Retain that design. Make the sparse LEAN phase-boundary checks actually consult cancellation, and define what Cancel means once the external execution has begun. If that phase is intentionally noninterruptible, expose that limitation and retain an explicit pending/too-late outcome instead of presenting an effective stop control. Do not infer that the container can safely be terminated from this review.

**Confidence. High** in the reproduced polling defect; **moderate** in the exact UI timing of a pre-start click, since the test deliberately controls scheduling. No timing assumption is needed for the source-proven absence of any post-start check. A separate functioning cancellation path or a UI capability gate that excludes LEAN would change the conclusion; none exists on the inspected path.

## J2 — An unknown persistence outcome is reported as definitely unsaved

**Claim.** The backtest writer can return no run ID while its insert continues, but Strategy Lab treats every missing ID as confirmed persistence failure and says the run was not saved.

**Goal.** Results should distinguish completed computation, confirmed save failure, and an unresolved save so users can recover the same result rather than unnecessarily rerunning it.

**Severity. Medium.** A delayed save can later appear in history after the interface said it could not. The practical consequence is confusion and unnecessary duplicate experiments; no overwritten result, double order, or false validation verdict was established.

**Evidence — proven.** The writer timeout is 60 seconds ([`persistence/db.py:28,49-51`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/persistence/db.py#L28-L51)). `run_on_background_loop` explicitly leaves the coroutine running and raises a distinct `CallerStoppedWaitingError` ([`background_loop.py:59-82`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/utils/background_loop.py#L59-L82)). The writer recognizes this distinction, logs “Run persistence outcome unknown,” but returns `None` when the insert has not yet reported completion ([`backtest_runs/service.py:97-125`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/backtest_runs/service.py#L97-L125)). That same return value represents confirmed payload/database errors. The response exposes a nullable `study_id` ([OpenAPI `EngineBacktestResponse.study_id`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/contracts/openapi/python-data-service.openapi.json#L14319-L14330)), not a persistence outcome. The frontend's Python and LEAN paths both state “The run was not saved to history” whenever their saved ID is missing ([`strategy-lab-runner.service.ts:620-647`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/strategy-lab/strategy-lab-runner.service.ts#L620-L647)).

**Evidence — reproduced.** [Synthetic tests](https://github.com/tim1016/learn-ai/blob/research/codex-2430-jobs/review/test_codex_2430_jobs.py), `test_unknown_save_returns_no_id_then_insert_finishes`, runs the real background-loop timeout and persistence service. Only the repository boundary is replaced with a delayed in-memory insert. With the same ordering compressed to a 1 ms wait and 50 ms insert delay, the caller gets `None`; the uncancelled insert subsequently completes with ID 2430. This is a behavioral reproduction of the unknown-outcome branch, not a claim that a real database write was performed. The static UI trace establishes the resulting message.

**Why it matters.** The user is advised to treat a recoverable result as absent. A manual rerun may create another experiment while the original is still landing. The existing history can make this discrepancy detectable, which bounds the severity.

**Rationale, quoted before recommending change.** The writer's stated policy is: “Persistence is best-effort by design and must stay so: a failure logs, yields `None` for the run id, and never fails the run that produced the payload.” ([`backtest_runs/service.py:3-9`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/backtest_runs/service.py#L3-L9)). The background loop deliberately avoids cancelling timed-out work, and the writer already returns a known committed ID when only parity settlement remains outstanding. These are useful decisions and are not challenged here.

**Recommendation.** Carry a distinct persistence outcome through the producer response and UI. For unknown outcomes, retain a correlation identifier and offer reconciliation with history; say that saving is unresolved. Preserve best-effort computation and the existing distinction between a writer wait timeout and a database-command failure. Do not require rerunning the backtest merely to rediscover its save.

**Confidence. High** in the producer behavior and UI mapping; **moderate** in operational frequency. The synthetic test shortens timing rather than measuring production latency. An independently correlated late-save notification that updates this page would mitigate the finding; none was found on the inspected consumer path.

## Positive controls and limits

- **The public job ID owns transport state.** .NET mints a fresh ID, stores the original parameters, injects the ID into the forwarded payload, and retrieves results by that ID ([`JobsApi.cs:104-173,273-287`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Backend/Jobs/JobsApi.cs#L104-L287)). It does not recompute strategy results. Strategy Lab prefers its per-tab job marker and adopts another job only when there is a single unambiguous candidate ([`strategy-lab-runner.service.ts:438-460`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/strategy-lab/strategy-lab-runner.service.ts#L438-L460)). The saved report restores persisted fields rather than leaving controls edited during the run beside the old result ([`strategy-lab.component.ts:151-179`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/strategy-lab/strategy-lab.component.ts#L151-L179)). Static trace; browser scheduling races were not exhaustively exercised.
- **Finish uses stored input.** Grid's resume branch loads the existing record, verifies resumability, and builds the worker from `load_search(search_id)` ([`grid_search.py:225-263`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/grid_search.py#L225-L263)); Walk-Forward mirrors that ownership ([`walk_forward_study.py:174-210`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/walk_forward_study.py#L174-L210)). Neither executes a new client's changed parameters as if they were the old receipt. Changed data/code identities and uncertain liveness refuse Finish ([`lifecycle.py:106-122`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/persistence/lifecycle.py#L106-L122)). This does not certify that on-disk code identity equals loaded process identity; that is separately investigated.
- **Writes are generation-fenced.** Claiming increments an attempt under row lock; later writes reject a missing, superseded or completed record ([`fence.py:98-137`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/persistence/fence.py#L98-L137)). Grid chunk/terminal writes and study progress/terminal writes use that fence inside their transactions ([`grid_search/repository.py:186-270`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/grid_search/repository.py#L186-L270), [`walk_forward_study/repository.py:120-160`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/walk_forward_study/repository.py#L120-L160)). This is source evidence; real concurrent Postgres transactions were not exercised.
- **Recorded data binds the read itself.** The cell adapter supplies the receipt's manifest to the engine ([`engine_adapter.py:85-95`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/grid_search/engine_adapter.py#L85-L95)). Existing synthetic snapshot tests passed for changed bytes, missing sessions, unreceipted files, minute/daily reads and equality to ordinary readers. The test does not rely solely on a before/after whole-study check.
- **Restart and liveness are explicit.** Startup fails orphaned queued/running jobs and removes their leases ([`progress.py:398-431`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/jobs/progress.py#L398-L431)); the dependent record presents interrupted when its job is no longer live. Existing in-memory tests passed for terminal events, expired leases, restart cleanup and unreachable stores. These tests assume the documented single service-process worker ownership; no deployment topology experiment was run.
- **Failure visibility survives the normal engine job path.** Its worker converts unsuccessful engine responses to job failure and checks cancellation at its phase boundaries and while waiting for the engine gate ([`jobs.py:504-542`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/jobs.py#L504-L542)). Four existing focused tests passed. Grid and Walk-Forward durable records retain their own failure/cancellation outcomes; transport completion alone is not an alpha verdict.

No new receipt/configuration substitution was established in this bounded pass. Remaining integration limits are actual Redis loss/reconnection, PostgreSQL locking and durability under process death, external LEAN termination, and browser concurrency. These were not simulated by starting shared or throwaway infrastructure. They are verification limits, not inferred failures. No new owner decision is required for the two recommendations; both can satisfy the documented lifecycle policy.

## Reproduction and verification receipts

Artifact: [review/test_codex_2430_jobs.py](https://github.com/tim1016/learn-ai/blob/research/codex-2430-jobs/review/test_codex_2430_jobs.py). From the isolated clone's `PythonDataService` directory:

```sh
env -i PATH="$PATH" HOME=/Users/inkant/codex-review-20260924/validation \
  PYTHONDONTWRITEBYTECODE=1 POLYGON_API_KEY=synthetic-review-key \
  DATA_PLANE_CONTROL_SECRET='' PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  TMPDIR=/Users/inkant/codex-review-20260924/validation/.review-tmp \
  /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B \
  /Users/inkant/codex-review-20260924/control/guarded_run.py \
  -m pytest -q --confcutdir=../review \
  --basetemp=/Users/inkant/codex-review-20260924/validation/.review-tmp/pytest-2430-repro \
  ../review/test_codex_2430_jobs.py
```

The literal Polygon key only satisfies settings construction; network was blocked before imports. **Two reproductions passed** in 2.41 seconds. Under the same guarded environment, **57 existing tests passed**:

| Targets | Outcome |
| --- | --- |
| `tests/jobs/test_job_lease.py`, `test_orphaned_jobs.py` with `--confcutdir=tests/jobs` | 17 passed, 0.01 s |
| `tests/research/sweep/test_snapshot.py`, `tests/research/backtest_runs/test_service.py`, `test_records.py` with `--confcutdir=tests/research -p pytest_asyncio.plugin` | 36 passed, 0.46 s |
| `tests/routers/test_engine_backtest_job_cancellation.py` with `--confcutdir=tests/routers` | 4 passed, 1.40 s |

Each batch used a separate review-local basetemp. No root broker fixtures or database fixtures ran. Warnings were dependency deprecations from imports and an unknown `asyncio_mode` option in batches where async plugin autoload was intentionally disabled. Full Python `app/`, `tests/` and the review artifact passed Ruff. Only this report and the review test are committed; no production fixes or PR were created.
