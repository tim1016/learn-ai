## Problem Statement

The owner wants to run thousands of backtests, including large Golden Search studies, on a machine primarily dedicated to this work. Evaluations currently repeat historical-data preparation and execute sequentially, leaving much of the machine's capacity unused.

The October 10, 2026 investigation observed a Mac with 128 GiB RAM and 18 logical CPUs, a Podman VM with 16 virtual CPUs and 32 GiB, and an API/backtest container limited to 2 CPUs and 2 GiB. The engine's process-wide gate intentionally permits one backtest at a time. Golden Search evaluates points and validation folds sequentially; its jobs run on threads inside the API process.

Representative unsaved replays from the active GLD EMA study measured:

| Evaluation | Total time | Historical read, validation and decoding | Evaluated minute bars |
|---|---:|---:|---:|
| Six-month training window | 0.752 s | 0.426 s | 47,769 |
| Full development window, approximately four years | 6.593 s | 3.736 s | 372,282 |

About 57% of elapsed time went into preparing data. These are representative observations under concurrent load, not a promised speedup. The short sample had no trades; the full-history sample had 29. The current local lake was approximately 177 MB and PostgreSQL approximately 95 MB. There is no measured requirement for a time-series database migration.

Completed-evaluation caching already exists. The missing capability is efficient execution of **different** evaluations that consume the same historical inputs.

## Solution

Run existing stock backtests through a bounded, persistent pool of Python processes in a **separate research-runtime container**, with reusable decoded historical data and enough resources to use this research machine effectively.

The researcher continues to use Golden Search, Grid Search, Walk-Forward and Recency. Existing study protocols, numerical results, evaluation identities, and resume rules remain authoritative. Progress explains whether work is waiting, preparing data, or evaluating, and how many evaluations are complete or reused.

Deliver a measured improvement before enabling the new executor by default. Start with four workers, qualify eight, and test twelve only when eight demonstrates useful scaling. Keep the API and broker clerks responsive.

**Release target:** at least 4× valid evaluation throughput with eight workers against the current serial implementation on the fixed warm benchmark described below; at least 3× for the same batch including derived-cache preparation. These are engineering acceptance targets, not results already achieved. Correctness, recovery and resource limits are independent release gates.

## User Stories

1. As the owner, I want large research studies to use multiple CPU cores, so that thousands of evaluations finish sooner.
2. As the owner, I want research to use substantially more of this machine's memory, so that artificial container limits do not dictate throughput.
3. As a researcher, I want different parameter combinations to reuse decoded historical bars, so that repeated preparation does not dominate each evaluation.
4. As a researcher, I want completed results reused under the existing full evaluation identity, so that a resume does not repeat finished work.
5. As a researcher, I want parallel and serial execution to produce the same trades and statistics, so that speed does not change the scientific answer.
6. As a researcher, I want the same candidates selected regardless of worker completion order, so that a fast worker cannot change the search.
7. As a researcher, I want finite budgets allocated in the same logical order as serial execution, so that parallelism cannot change which evaluations fit.
8. As a researcher, I want adaptive rounds to wait for the results they depend on, so that Zoom preserves its procedure.
9. As a researcher, I want independent ready evaluations from multiple studies to share capacity fairly, so that one long study does not monopolize the machine.
10. As a researcher, I want final-test reservations and authorization preserved, so that parallel preparation cannot expose held-out results.
11. As a researcher, I want each simulation to have independent indicators, portfolio, orders and random state, so that one evaluation cannot contaminate another.
12. As a researcher, I want cached bars checked against current admission and adjustment evidence, so that a warm cache cannot admit revoked inputs.
13. As a researcher, I want missing, changed or corrupt source evidence to fail visibly, so that performance improvements cannot conceal incomplete history.
14. As a researcher, I want raw, adjusted, regular-session and extended-session data kept distinct, so that incompatible bars cannot share a cache entry.
15. As a researcher, I want warmup and calendar behavior preserved exactly, so that a faster reader does not alter the evaluation start.
16. As a researcher, I want Grid Search, Walk-Forward and Recency to use the same bounded execution capability, so that each feature does not create its own unaccounted pool.
17. As the owner, I want queued, preparing, active, completed and reused work shown accurately, so that I can understand progress.
18. As the owner, I want Cancel to stop dispatch and release resources promptly, so that I can recover capacity without restarting the application.
19. As the owner, I want a crashed worker reported truthfully, so that a dead process cannot leave a study falsely running.
20. As the owner, I want Finish to reuse durable completed evaluations after interruption, so that a worker failure does not discard the study.
21. As the owner, I want stale worker results rejected after cancellation or a newer attempt, so that old work cannot overwrite current evidence.
22. As the owner, I want browser closure to leave authorized research running, so that the UI is not the job's supervisor.
23. As the owner, I want a resource limit that accounts for workers and caches together, so that increasing parallelism cannot exhaust memory.
24. As the owner, I want other pages and broker clerks to remain responsive during research, so that the machine remains usable.
25. As a researcher, I want summary evaluations to retain their required metrics, trades and daily curves, so that reduced transport does not weaken evidence.
26. As the owner, I want a reversible rollout and explicit drain procedure, so that an executor change does not disrupt an active study.
27. As the owner, I want measured valid evaluations per hour at several worker counts, so that I can select capacity from evidence.
28. As the owner, I want TimescaleDB or another dependency introduced only for a demonstrated requirement, so that infrastructure remains proportional to the workload.
29. As a maintainer, I want small evaluation requests and bounded results between processes, so that serialization does not erase parallel gains.
30. As a maintainer, I want existing numerical and lifecycle tests to remain authoritative, so that this work improves execution without creating another engine.
31. As a researcher, I want exam evidence to survive worker replacement and process boundaries, so that valid detail results do not become spurious missing-evidence acknowledgements.
32. As the owner, I want progress to explain adaptive dependency waits, so that a narrow search round is distinguishable from unavailable machine capacity.
33. As the owner, I want admission queries and database connections bounded across all workers, so that faster research does not overwhelm shared services.

## Implementation Decisions

**Deployment and ownership — separate research container**

- The first release targets the existing Python stock engine and its research callers: Golden Search, Grid Search, Walk-Forward and Recency. Deliver Golden Search first, then integrate the other research callers before closing this PRD.
- Run one research coordinator and its persistent, explicitly spawned worker processes in a **separate container and cgroup**. The eight-worker qualification envelope is eight CPUs and 16 GiB for this entire container. The API and every clerk process remain outside it, including deployments using the combined API/clerk posture. An in-API child pool with raised API limits does not satisfy this decision.
- The existing Python study/procedure modules execute in the research coordinator; they remain the sole study authority. The API forwards research commands across an authenticated internal service boundary. Existing job IDs, Redis progress/cancellation and Python-owned research persistence remain authoritative. An internal transport hop is allowed; no new broker, queue product, results database or scheduler framework is introduced.
- Command delivery is idempotent under the existing job/study identity and attempt fence. An HTTP timeout is not proof that dispatch failed; reconcile ownership before retrying. The coordinator owns capability checks, deterministic reservations, dispatch, acceptance, persistence and selection. Workers own only isolated computation and disposable decoded data.
- Workers have no database/Redis clients or pools, schema initialization, provider downloads, broker connections or study mutations. They read local source artifacts and communicate through bounded private IPC. Use a minimal worker entry point rather than importing the API application or broker startup graph. All catalog access and persistence stay in the research coordinator, with the isolated admission path described below.
- Interactive Strategy Lab and other nonresearch engine callers retain their current execution path in v1. Their existing container allowance is reserved separately and included in whole-machine measurements. No route may create an unbounded private pool.
- Worker lifetime belongs to the research container, not the API. An API restart leaves a healthy research job running. A research-coordinator restart interrupts its own attempts and requires explicit Finish; it never auto-resumes them. Persist the runtime owner and boot generation on new job ownership records so startup recovery is scoped: API startup closes only API-owned jobs; research startup fences abandoned research generations after their workers are stopped. Legacy unscoped jobs retain the legacy API-owned recovery rule.
- Run one active coordinator for this runtime. A replacement cannot claim the same authority while the former coordinator remains live. On coordinator exit, stop/reap its children; on container shutdown, stop dispatch immediately, allow at most ten seconds for cooperative cleanup, then kill/reap remaining processes, including children still importing. Hard shutdown leaves recoverable pending evaluations, not a promise that a whole study will drain in ten seconds. An operator-requested drain precedes planned capacity/version changes.
- Refuse enabling pooled execution with development reload, including the API launch configuration using `uvicorn --reload`; refuse reload in the research service itself. This is a startup error, not a warning.

**Existing module boundaries and the process-local gate**

- Retain the injectable `ExecuteBacktest` contract for one evaluation and the existing response projection. Add bounded submission/collection at the study evaluator's ready-batch boundary, where the complete ordered candidate sequence is known. Merely replacing a synchronous callable inside a serial loop does not provide parallel dispatch.
- The research coordinator **never acquires `one_backtest_in_flight` while submitting or waiting for worker execution**. The current outer service gate and inner engine gate execute inside each worker; each is process-local. Direct API callers retain the API's own gate. Dispatch runs outside an event-loop thread, as the gate contract already requires.
- Preserve the rule that a gate holder must not delegate engine work and wait; restate it explicitly for process dispatch. Do not pass gate objects or inherit held locks. Reshape the existing one-at-a-time test to prove serialization within a process and allowed overlap across workers.
- Extend the option-search precedent: explicit spawn, plain bounded requests/results, no inherited live connections or loops, tracked child ownership from creation, startup/work deadlines and kill-and-reap cleanup. Reuse the shared attempt fence and canonical reservation transaction; do not build another budget authority.
- Run materialization remains the owner of partial-coverage acceptance under ADR 0049 D3a. For pooled research, invoke that canonical policy in coordinator preparation before dispatch; the worker receives only the admitted manifest and capability. Golden Search's sealed-input path continues to refuse incomplete required coverage. A worker/cache may not fetch, silently narrow a window or decide that a partial result is acceptable. Preserve each existing caller's policy when integrating the remaining research surfaces.

**Reusable decoded artifacts without weaker admission**

- Start with a byte-bounded process-local cache of immutable **per-artifact** decoded bars. A minute-day artifact or whole-symbol daily-history artifact is the unit of reuse. A window is an ordered composition of artifact references, not a distinct decoded-cache entry.
- An artifact key includes root identity, root mode and canonical relative path, exact content digest, instrument/resolution, adjustment mode and relevant current adjustment/companion evidence, session policy, calendar identity/version and decoder/schema version. It excludes study ID, point, scored window and run-up length. Window coverage and run-up remain in the manifest/evaluation identity; overlapping folds reuse the same artifact entries.
- Keep the planned run-up for the whole legal search space under ADR 0074. Different fold boundaries select/subset admitted artifacts without decoding identical days again. Report duplicate residency across workers honestly: this is one decode per worker/cache residency, not one decode globally.
- Use the existing pre-fingerprinted-bytes seams `parse_day_zip` and `parse_history_zip`. Before each artifact read can use a decoded hit, read/fingerprint the source and preserve the baseline check order: committed-byte admission, manifest comparison, current adjustment/companion verification, then decode-or-cache reuse. Fresh iterators expose the same ordered immutable values.
- Revocation with unchanged file bytes, mid-evaluation revocation before a later artifact read, adjustment-basis changes, missing root markers, changed companions, missing sources and unavailable catalogs retain the baseline refusal behavior. No retroactive invalidation of already accepted completed evaluations is added.
- Fix the `LeanDailyDataReader._history_cache` early-return path in the cache slice: a hit must pass the applicable admission and adjustment chain before reuse, including the manifest-bound reader override. It is not an approved precedent or exception. Preserve multi-root precedence and the existing unmanaged-reference-fixture behavior.
- Cache eviction cannot invalidate active iterators. If an artifact cannot be retained within the allowance, decode through the bounded uncached path under the same checks. Current simulations, retained objects and result buffers count toward total memory even after a cache entry is evicted.
- Prefer warm worker affinity among otherwise eligible workers, subordinate to fair share and ready capacity: never leave an available slot idle solely to await a warm worker. Measure misses, evictions and duplicated bytes for the actual assignment policy.
- Prepared data is disposable and never a new admission authority. If measurements later justify durable derived files, version, checksum and publish them atomically, protect in-use artifacts from collection, and explicitly invalidate/rebuild corruption from admitted sources.
- PyArrow and NumPy are already available. Benchmark Arrow IPC or an exact numeric representation only if private memory, serialization or decoding still limits scaling. Preserve Decimal values exactly; mapped buffers alone do not make the object-based engine zero-copy.

**Admission throughput and connection budget**

- Treat admission as a measured critical path: the observed full-window replay performed 961 artifact admissions, and decoded-bar reuse removes none of their receipt checks. Include source reads/hashing, catalog wait, query execution and admission IPC separately in measurements.
- Route workers' current artifact-admission requests through a bounded coordinator admission service. Each request binds the exact root/mode/path, digest, byte length and task generation for bytes the worker has just read. Return a decision only for that read; do not turn it into a reusable positive permit. Preserve per-worker consumption order and run the manifest/adjustment checks before yielding bars.
- Batch **concurrently pending read-boundary requests** into one set-based catalog lookup, deduplicating identical requested tuples within that batch and returning a decision to every requester. Do not wait to fill a batch or pre-authorize future artifacts. Start with at most one pending artifact request per active worker, and bounded query concurrency.
- A fresh bulk receipt lookup once per window is **not** equivalent to today's per-artifact admission: a later artifact may be revoked after the bulk lookup but before its read. A bulk lookup may support planning, but cannot authorize those later reads. Therefore this PRD does not claim a 961-to-one reduction per evaluation; report achieved batch sizes and round trips. Once-per-window or once-per-study admission requires a separate snapshot/revocation decision and is excluded here.
- Allocate at most **12 PostgreSQL connections to the entire research runtime** initially: up to eight for the admission service and four for study/materialization/persistence work, with zero worker-owned connections. This is a total across loops/pools, not a per-pool default. Reserve writes/cancellation handling from admission starvation.
- Before rollout, inventory configured maxima for the API, Backend, clerks, research and maintenance against the deployed server limit. Leave at least 20 connections unallocated for operational headroom; reduce research admission concurrency if the budget does not fit. Do not multiply today's default ten-connection pool by worker count or silently raise PostgreSQL's connection limit.
- Keep admission I/O off the API/fleet shared loop and off the study-writer loop. Use an explicitly owned bounded I/O path within the research coordinator; move source hashing/decoding and other blocking CPU work out of that loop. F13's shared-loop/GIL serialization is a warning to verify concurrent latency, not a reason to give pure workers database access.
- Preserve typed catalog-unavailable errors and bound admission calls below the task-silence deadline. An outage fails the affected reads visibly; it must not retry the same timeout once for every day in a long window.

**Parallelism, identity and deterministic accounting**

- Parallelize complete independent evaluations, never the time-ordered bar loop within a simulation. A Zoom round's ready points may execute concurrently; its next round/coordinate waits for the same dependencies as serial execution.
- Keep folds sequential within one study in v1; parallelize their ready point batches and work from different studies. Preserve canonical point order, duplicate/invalid-point handling, tie-breaking and strict improvement.
- Reserve new evaluations atomically in serial logical order before dispatch. If only a prefix fits, dispatch exactly that prefix. This applies to grid batches, Zoom batches, exam fits, recency and **pair-audit cells**. Preserve proof/exam reservations and existing retry accounting; no speculative work consumes budget or exposes results beyond the admitted prefix.
- Deduplicate in-flight work by the **full existing evaluation key**: stable context digest, point hash, window bounds, scenario and detail flag. Match ownership to the live study attempt/task generation. A summary request cannot satisfy a detail request; identical points in different windows/scenarios cannot join. Preserve the existing charge and cache-hit accounting for each distinct key. Do not introduce cross-study sharing of an in-flight reservation in v1.
- Accept/persist successful outcomes as they arrive under the current fence. Logical result consumption, recorded procedure order and winner selection follow serial input order; operational completion timestamps can differ. Independent failures become the same recorded failed evaluations as today. Cancellation at the same wall-clock instant need not produce the same completed prefix.
- Carry the coordinator's sealed code/environment identity, full evaluation key, attempt/task generation, allowed window/scenario, manifest reference, parameters and detail level in every task. Workers validate the sealed identity against their own loaded runtime before computing and refuse a mismatch; they never replace it with a newly derived identity. Preserve the existing exclusion of provenance-only volatile Git fields from scientific hashes.
- Use Python 3.12's explicit spawn context. Set a fixed `PYTHONHASHSEED=0` and `OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=1` in the research launch environment **before interpreter startup and native imports**. A worker initializer is too late to set Python's hash seed. Bound Arrow CPU/IO pools when used; record effective settings and verify them at startup. Preserve canonical sorting and any task-scoped random seed independently of hash seeding.
- IPC and durable temporal fields use int64 milliseconds UTC; warmup/session boundaries derive from the canonical calendar. Do not send language-native date objects across the worker boundary.
- Initialize fresh strategy, indicator, portfolio, order and random state for each task. Decoded buffers may survive between tasks; manifest bindings and adjustment-version pins are per evaluation, never worker-lifetime state. No nested process pools. Transport small requests and bounded results, not full bar lists or arbitrary application objects.
- Start with one evaluation per dispatch message. Measure transport overhead before considering small message batches; any batching may contain only already-reserved independent tasks, must stream each result/checkpoint separately and retain per-task cancellation/fencing. Never batch across an adaptive dependency to fill workers.

**Durable detail and coordinator persistence**

- Keep the existing summary/full projections and required metrics, trades, daily curves and minute-equity statistics. Full detail is requested under its own evaluation identity and persisted by the coordinator before dependent exam work proceeds.
- Make `StudyEvaluator.detail_of` a repository-backed read of the completed detail evaluation under the exact study/context, point, window, base scenario and detail identity. A process-local dictionary may be an optimization, never the only source of chance/drawdown evidence. Readback uses no engine evaluation and spends no new budget.
- Test full detail delivered from a worker and read after evaluator replacement/Finish. A completed detail record with absent/corrupt required payload is an explicit evidence-integrity failure; it must not silently become the scientific “missing” reading that demands a research-weakness acknowledgement. Preserve legitimate protocol-defined missing/insufficient readings.
- All reserve/complete/release/progress/terminal writes remain coordinator-owned and fenced. A background-loop caller timeout can mean its database work is still running; `CallerStoppedWaitingError` is an unknown outcome, not proof of rollback. Reconcile the original operation and durable evaluation/task generation before retrying or releasing its reservation. No double budget increment, acceptance or stale terminal write is permitted.

**Checkpoint-based liveness, cancellation and recovery**

- ADR 0065's 300-second public lease remains the liveness authority, but pooled renewal is tied to **new accepted progress evidence**. A coordinator timer, process-existence check, status read, cancellation poll, repeated checkpoint or repeated capacity check never renews it by itself. Separate read-only cancellation polling from today's renewal side effect.
- Each checkpoint carries job/study attempt, worker/task generation, strictly increasing sequence, phase and a monotonic work cursor such as artifacts admitted, bars processed or results persisted. Only the current owner accepts it. A repeated “still running” event without advancement is not progress.
- Accepted worker checkpoints and actual coordinator transitions (reservation/dispatch, result acceptance, successful durable completion) can renew the lease once. Do not replay the last checkpoint from a timer. Queued jobs can renew on actual fair-queue advancement/capacity handoff; merely inspecting a full pool is insufficient.
- Apply a silence deadline independently to **every active task**, so a healthy worker cannot hide a hung sibling. On expiry, fence the task, kill/reap that worker and follow the existing retry/interruption policy. A queued attempt whose queue makes no progress within its bound becomes visibly interrupted/capacity-blocked and releases pending claims; it is not kept live indefinitely.
- Use the following initial bounds, all below the public lease TTL. Any qualified change to a bound must remain below that TTL and be recorded in the operator configuration and benchmark; a phase cannot gain indefinite extensions by changing its label.

| Phase | Evidence that advances | Initial bound |
|---|---|---|
| Worker startup/import | Successful ready handshake; no assigned evaluation before readiness | Ready within 60 s or kill/reap; import chatter does not extend it |
| Queued | Real queue advancement or capacity handoff | No-progress bound 60 s; cancellation polling at most 1 s |
| Preparing/admitting | Next verified artifact or completed preparation unit | Checkpoint at least each completed artifact/unit; no silent unit over 60 s; catalog call deadline at most 35 s |
| Simulating/aggregating | Increasing processed-bar/work-block cursor | Checkpoint target at most 1 s between completed blocks; no-progress bound 30 s |
| Accepting/persisting | New accepted result or confirmed durable operation | Operation deadline at most 60 s; uncertain writes enter reconciliation, not fictitious progress |

- Internal checkpoints also provide bounded opportunities to observe cancellation. On Cancel, stop new dispatch within one second of receipt; release every task's execution slot within ten seconds, terminating a worker that does not cooperate. Blocked admission/database I/O cannot block the cancellation control path. A continuing uncertain write is fenced and reconciled separately from execution-slot cleanup.
- A silent or dead worker may retry the same reserved evaluation only within the existing allowance and a still-live attempt. Only one completion can be accepted for the current evaluation/task generation. Reject late delivery after cancel, timeout, retry or attempt supersession.
- Never recreate an expired lease. A lost coordinator or abandoned runtime generation becomes interrupted; Finish claims a new job/attempt under the existing identity checks. Browser closure and API restart do not stop a healthy research coordinator. Research-runtime restart does, as specified in the topology decision.

**Resource allocation and rollout**

- Configure worker count, aggregate memory, cache bytes, pending tasks and result-buffer limits together. Use fair scheduling across studies and a small bounded dispatch window; never submit a study's entire 5,000-evaluation budget into resident futures.
- Qualify 1, 2, 4 and 8 workers. Start deployment with four; enable eight after qualification. Twelve is optional only if measured throughput improves within a separately declared resource envelope.
- The initial research container has `pids_limit=512`. Count Linux tasks, including native threads, not just Python processes: coordinator/control threads + spawn helpers + workers × (main thread + configured native/IO threads). Measure cold-import and busy peaks; qualification must stay below 80% of that limit. Fail startup if the configured pool cannot fit, rather than changing the API/fleet PID caps.
- Account for worker imports, decoded caches, current simulations, IPC/result buffers, admission and coordinator memory together. Measure container memory plus private/shared mappings; do not treat the earlier 438-MiB interpreter peak as a universal worker bound.
- Use eight CPUs/16 GiB for the initial eight-worker qualification. The existing 16-vCPU/32-GiB VM is acceptable if the entire deployed stack fits with headroom; 16 vCPU/64 GiB is an expansion target. Measure PostgreSQL and sibling-container resources too. Separate cgroups do not excuse exhausting the VM.
- Under memory pressure, evict unused cache entries or reduce dispatch. Never shorten input data, drop evidence or move the API into the research cgroup. Set lower defaults for smaller machines.
- Roll out behind an operator-selected executor setting. Drain before planned runtime/VM/version changes. Rollback directs new research commands to the serial path; active jobs retain their existing owner until drained/interrupted. Durable results and identities remain readable.
- Do not alter the currently running study merely to enable this feature. Existing unfinished studies resume only if recorded code/data identity is compatible; explain refusal instead of rewriting their receipt.

**User experience and observability**

- Reuse study views and job progress for queued, preparing, evaluating, completed, reused, failed and interrupted counts. Distinguish “waiting for worker capacity” from “waiting on an adaptive dependency.” Raw cache keys and process internals stay out of normal user flows.
- Preserve upper-bound-total semantics. Result-cache hits are not newly executed backtests. Report why capacity is unavailable and whether work is progressing.
- Coalesce ordinary Redis/SSE progress to at most one update per second per study, independently of internal checkpoint frequency. Always deliver terminal transitions and required errors. Bound payloads and stream retention; checkpoint traffic must not scale into one public event per artifact/bar.
- Measure queue wait, preparation, validation, decoding, simulation, aggregation, request/result serialization, IPC, durable writes and admission round trips separately, avoiding addition of nested timings. Record valid new evaluations/hour, cache hit/miss/eviction and duplicate residency, worker restarts, memory/PID/connection peaks and event volume.

**Required ADR amendments in the implementing slices**

- Amend **ADR 0055's one-backtest-in-flight amendment** in the runtime slice: direct callers remain serial per process; research uses the bounded separate process runtime, and its coordinator never holds the engine gate while waiting on a worker.
- Amend **ADR 0065 D1/D2** in that same slice: checkpoint-based renewal, phase/silence bounds, independent hung-task detection, separated cancellation reads, owner/boot-generation-scoped recovery and the chosen API-versus-research restart behavior.
- Amend **ADR 0049 D3a** when the materialization boundary moves: the same canonical run-materialization policy is called by research-coordinator preparation; workers do not own acceptance or acquisition.
- Record the container/transport/resource decision and update the **engine authority map**, including the process-level gate rule and coordinator/worker responsibilities. Changes to these accepted authorities belong in the PRs that introduce the behavior; this PRD alone does not supersede them.

## Testing Decisions

Test externally visible outcomes through the existing execution/evaluator and study lifecycle seams. Preserve the canonical engine, reader checks, shared attempt fence and repository; avoid tests that only inspect private cache fields or the presence of a pool.

Parameterize the existing Golden Search lifecycle scenarios over the current serial executor and a deterministic in-process batch adapter for quick CI. Run the corresponding pool-backed scenarios in the daily tier using real spawned workers, an ephemeral catalog/research database and a seeded local lake. Retain `ExecuteBacktest` as the single-evaluation injection point, with the bounded batch adapter at its caller; a fixture swap must exercise actual concurrent submission, not just move one serial call to another process. Reuse the option-search suite's existing real-process startup, hang and cleanup harnesses.

| Seam | Required externally visible evidence | Existing prior art |
|---|---|---|
| Canonical execution and projection | Cached/uncached and serial/parallel paths produce identical ordered bars, trades, fills, fees, metrics and required curves | Engine goldens, Grid Search adapter parity, summary/full parity |
| Verified bytes to decoded artifact | Overlapping fold/run-up windows reuse days; every hit still passes admission, manifest and current adjustment checks; daily-history hits obey the same contract | Snapshot readers, lake admission, adjustment-version and daily-reader tests |
| Admission batching and resource control | Revocation before a later read is refused even when earlier reads were admitted; batched decisions equal individual checks at their read boundaries; outages fail visibly; bounded connections and concurrent latency | Catalog receipt tests and F13 concurrency methodology |
| Study evaluator and ready batches | Completion permutations preserve canonical output/selection; prefix exhaustion covers grids and pair audits; windows/scenarios/detail remain distinct | Golden Search evaluator, procedure, budget and selection suites |
| Repository and shared attempt fence | Duplicate delivery, uncertain writes, retry and late completion cannot double-count or overwrite newer attempts | Repository integration, attempt-fence and injected lifecycle scenarios |
| Exam detail readback | Worker-delivered detail remains available after evaluator replacement/Finish; chance/drawdown readings and required acknowledgements match serial; missing/corrupt expected detail fails explicitly | Exam stages, detail projection, durable evaluation cache |
| Worker runtime and environment | Same evaluation in two fresh workers and the serial control has identical result/trade-log/metrics hashes; mismatched sealed identity is refused | Code-identity tests and option-search spawn/kill/reap tests |
| Gate and container boundary | One engine run per worker process, concurrent overlap across processes, no coordinator gate hold; research restart/OOM cannot kill API/clerk processes | Engine run-gate suite and isolated deployment qualification |
| Lease, cancellation and owner-scoped recovery | A hung worker cannot be kept live by a timer, cancel polls or healthy siblings; stale boot/task generations cannot renew; API restart preserves healthy research; research restart requires Finish | ADR 0065 job lease/cancellation/resume suites |
| Progress and user experience | Capacity waits and adaptive waits differ; totals/cache counts stay honest; SSE is coalesced without losing terminal states | Job-progress and study-view tests |

Additional required cases:

- Preserve strict equality for timestamps, ordered input values, Decimal values, signals, trades and integer outcomes. Preserve existing floating-point equality or explicitly pinned golden tolerances; never widen tolerances for concurrency.
- Cover trade-heavy and no-trade inputs, raw/adjusted data, overlapping windows and run-up depths, DST, early closes, missing-bar rejection, multi-root daily precedence and unmanaged reference fixtures. Perturbing held-out inputs cannot affect development results.
- Warm a cache, revoke a later artifact's receipt without changing its bytes, then reach that artifact in the same evaluation: it must refuse. Repeat for changed companions/current adjustment basis, missing marker/source and catalog outage. An overlapping-window hit test must prove decoded reuse without skipping these checks.
- Reverse/randomize completion order; duplicate a full key; submit the same point with different window, scenario or detail; force grid and pair-audit budget exhaustion. Assert the canonical reserved prefix and selected outcome match serial.
- Kill an actual worker at controlled reservation/compute/delivery seams, including the last admitted point near budget exhaustion. Compare reservations, retry use and final durable records with the equivalent serial failure schedule. Test late results after retry and after Finish claims a newer attempt.
- Simulate a database caller timeout while the original write later commits. Assert reconciliation prevents duplicate reservation/completion and that cancellation still releases execution slots.
- Freeze one worker while another continues, then freeze all workers while the coordinator timer and cancellation polls keep running. Assert independent deadlines, bounded cleanup, no artificial lease renewal and truthful Finish eligibility.
- Test queued cancellation, bounded queue stalling, coordinator death, API-only restart, cold-import shutdown, lease expiry, redelivery, reload refusal and no orphaned children. Use isolated containers to test a research cgroup kill; never induce an OOM in the live stack.
- Test completed detail readback after destroying the evaluator's in-memory state, without an extra engine call or budget charge. Assert the exam's acknowledgements exactly match serial output.
- New real-process/infrastructure tests are `slow`-marked for the daily tier. Quick CI exercises semantics with injected adapters. No test contacts a broker or writes a clerk store. No new CI jobs; keep existing job time budgets, rebalancing slow tests if necessary.
- Scheduling does not justify changing numerical code, mathematical versions or historic hashes. Any actual math change follows the repo's provenance, golden-fixture and parity contract. Apply the repository's independent-review rules to each implementing PR.

**Performance acceptance and benchmark publication**

Freeze the corpus, designated end-to-end study and expected scientific outputs before optimization. Use **1,000 distinct uncached evaluation identities: 200 full-development and 800 fold-window evaluations**, covering sparse and trade-heavy results and several strategy/parameter groups. The earlier 0.752-second fold replay motivates overhead measurement; it is not an assertion that all 800 cases will have that duration.

The frozen study record must publish points-per-round histograms for each stage/fold: sampled, valid/testable, already cached/duplicate, newly reserved and actually ready in parallel. Record adaptive dependency time and the dependency-limited concurrency ceiling. Include both one-study and multiple-independent-study workloads; do not present aggregate throughput as single-study acceleration.

Existing evidence already shows why this matters. In the captured GLD EMA study `b5c9dc347c885886162b9348e0bb7987`, Search and Recent each recorded 16 Zoom rounds. **For each stage**, testable widths were:

| Testable points in a round | Number of rounds |
|---:|---:|
| 3 | 5 |
| 5 | 8 |
| 6 | 3 |

The mean is 4.5625 testable points and the maximum is six, before removing result-cache hits and duplicates. The saved snapshot does not identify new uncached width per round, so this is not an eight-worker speedup prediction or the frozen qualification corpus. The qualification trace must supply that missing distribution.

Run the same frozen corpus against the current serial implementation and each qualified worker count, with at least three repetitions. Report medians/variability, exact code/environment/dataset identity, limits and competing load. Include a **same-resource serial control**, plus serial with decoded-cache reuse, to separate resource allocation, data reuse and parallel execution.

“Warm” means derived data is reusable on the workers actually selected, with required admission still enabled. Report cache residency/hit rates rather than assuming one worker's warmup warms the pool. “Preparation-inclusive” starts with empty derived caches and includes pool startup, imports and derived-cache construction. Source files remain local/admitted; provider acquisition is separate. Do not flush host caches or disturb a production run to manufacture a cold case.

Publish the following alongside throughput:

- Per-evaluation p50/p95 request serialization, queue time, IPC transfer, result serialization and persistence latency, plus startup amortization. Report transport/persistence as a fraction of the short-fold wall time; 50–100 ms is material against the observed 0.752-second sample. Include detail payload bytes and memory.
- Receipt requests versus SQL round trips, observed batch-size distribution, admission p50/p95 at 1/2/4/8 workers, connection peaks, pool waits and study-write latency. No assumed 1,000-fold reduction and no hidden worker database pools.
- Cache hit/miss/eviction by artifact and worker, duplicated residency, ready/adaptive-wait/queue-wait time, CPU use, container and VM memory, PID/native-thread counts, SSE volume and worker cleanup.
- The same scientific outputs and admission refusals under all executor/cache modes. Cache hits for completed evaluations are reported separately and do not count as new throughput.

Release gates:

- Eight workers achieve at least **4× warm** and **3× preparation-inclusive valid new evaluations per hour** versus the original serial baseline on the fixed corpus.
- The designated end-to-end Golden Search protocol is scientifically identical and completes at least **2× faster**. Report its ready-width distribution and adaptive waits; aggregate multi-study throughput cannot substitute for this gate.
- All runs fit the declared CPU/memory/PID/connection envelope with no unbounded growth or leaked worker/task/dataset references. Concurrent admission measurements must show where contention lies; if an adapter serializes the pool, resolve it before default rollout.
- Under the eight-worker benchmark, lightweight API reads have p95 below one second and no unexpected errors; no broker clerk restarts occur. Include whole-VM headroom in the result.
- Cancellation meets the one-second dispatch-stop and ten-second slot-release bounds, and the fault/restart/correctness tests pass.
- If any target fails, publish the measured bottleneck and keep the executor opt-in. Do not change the corpus, count result-cache hits or weaken admission to declare success.

Long throughput/deployment qualification runs manually on the research machine, outside time-bounded CI. Publish its raw measurements and summary on this issue or a linked throwaway research branch.

## Out of Scope

- TimescaleDB, ClickHouse, a distributed cluster, Ray, Celery, GPUs, or a new market-data provider.
- A new numerical engine, relaxed equivalence, Decimal-to-float conversion, new selection criteria or altered search budgets.
- Skipping current admission/revocation checks or allowing detached old datasets to continue after source revocation.
- Parallel execution of the sequential bar timeline or adaptive folds within one study in v1.
- Intraday options replay implementation from #3030. Its future worker must satisfy this execution contract, but that feature remains separate.
- A new automatic research scheduler, automatic study resumption, automatic candidate approval, or automatic live deployment.
- Changing IBKR's retained live-data role, buying Alpaca market data, or deleting, moving or rewriting clerk stores or money trails.
- Source-catalog recovery by scanning derived caches; canonical recovery remains governed by existing decisions.

## Further Notes

**Owner direction:** this machine is predominantly intended for research; substantially greater CPU/memory allocation is acceptable. The selected worker count follows measured scaling, including single-study dependency limits.

**Delivery order:** (1) freeze corpus, expected outputs, ready-width trace and connection/resource inventory; (2) introduce per-artifact reuse and fix daily-history admission on hits; (3) deliver Golden Search through the isolated runtime, admission batching, deterministic reservations, durable detail and checkpoint-based recovery with the required ADR amendments; (4) integrate Grid Search, Walk-Forward, Recency and progress; (5) publish qualification, document drain/rollback and enable the qualified default. Benchmark admission and IPC in the first pooled vertical slice rather than deferring them until all callers are integrated.

**Review disposition:** this revision resolves the architecture and acceptance requirements raised in the [adversarial review](https://github.com/tim1016/learn-ai/issues/3034#issuecomment-6103963265). It adopts separate-container isolation, the coordinator gate prohibition, progress-backed leases, composable artifact keys, durable detail, full-key deduplication and explicit ADR amendments. It tightens three implementation suggestions: window-wide receipt prechecks cannot replace later per-read checks; Python's hash seed must be set before interpreter startup; and the synchronous executor needs a ready-batch caller to achieve concurrency. Real-process test precedent already exists in option search.

The durable PRD is this issue. Local Markdown is a review artifact, not documentation to merge into master. Accepted decisions and operator procedures become ADRs/runbooks in their implementing PRs; benchmarks and investigation notes remain on the issue or a throwaway research branch. This revision changes the specification, not a running study or deployed capacity.

Relevant standing decisions: [research ownership and serial execution, ADR 0055](https://github.com/tim1016/learn-ai/blob/6fb42b4554daad9a349c95410f40b89c1552b887/docs/architecture/adrs/0055-python-owned-research-tables.md), [data lake authority, ADR 0049](https://github.com/tim1016/learn-ai/blob/6fb42b4554daad9a349c95410f40b89c1552b887/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md), [worker lease, ADR 0065](https://github.com/tim1016/learn-ai/blob/6fb42b4554daad9a349c95410f40b89c1552b887/docs/architecture/adrs/0065-research-job-liveness-is-a-worker-lease.md), [research identity, ADR 0072](https://github.com/tim1016/learn-ai/blob/6fb42b4554daad9a349c95410f40b89c1552b887/docs/architecture/adrs/0072-research-run-identity-and-sealed-run-inputs.md), and [Golden Search, ADR 0074](https://github.com/tim1016/learn-ai/blob/6fb42b4554daad9a349c95410f40b89c1552b887/docs/architecture/adrs/0074-golden-search-qualified-configurations.md). ADR 0055's serial-execution amendment and ADR 0065's global startup sweep must be amended explicitly as specified above, not bypassed by implementation.

Primary references: [Python 3.12 process executors](https://docs.python.org/3.12/library/concurrent.futures.html#processpoolexecutor), [Python startup environment and hash seeding](https://docs.python.org/3.12/using/cmdline.html#envvar-PYTHONHASHSEED), [Arrow 19 Parquet decoding and memory mapping](https://arrow.apache.org/docs/19.0/python/parquet.html#reading-parquet-and-memory-mapping), and [Arrow IPC](https://arrow.apache.org/docs/19.0/python/ipc.html). These establish capabilities, not achieved learn-ai speedups.

TimescaleDB remains a future option if shared historical querying, ingestion or retention becomes a measured requirement after these improvements. A migration would need a separate proposal and explicit treatment of ADR 0049's files-as-authority decision.
