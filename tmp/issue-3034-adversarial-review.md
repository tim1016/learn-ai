# Adversarial review: what the PRD gets right, and the decisions it still leaves open

I traced every seam this PRD names against the current tree — the evaluator dispatch path, the reservation transaction, the manifest-bound reader, committed admission, the lease, the fence, the run gate, the compose topology, and the test suites — plus the four ADRs it cites and the Oct-10 measurements in `output/backtest-throughput-research-2026-10-10.md`. The PRD is unusually well grounded; most of its correctness language matches the code's actual contracts almost line for line. The improvements below are therefore not "redo the plan" — they are the places where a sentence in the PRD silently decides architecture if left unstated, plus three throughput eaters the release targets don't yet account for.

## What the PRD gets exactly right (keep, and lean on these modules)

The codebase already contains the well-architected seams this feature should be assembled from. Naming them protects the design from being rebuilt wrong:

- **The reservation-prefix rule matches serial truncation exactly.** Serial order is the per-point loop in `StudyEvaluator.evaluate` (`app/research/golden_search/evaluator.py:341`), each point reserved in input order inside one fenced transaction (`app/research/golden_search/repository.py` `reserve_evaluation`: row-lock the study, `consumed + 1 > limit` → exhausted). "If only a prefix fits, dispatch exactly that prefix" is precisely equivalent. The budget counter is already process-safe (row lock + CHECK constraint), so parallelism only needs to preserve *order*, not invent locking.
- **The workers-are-pure shape already has a precedent**: `app/services/option_search.py` (ADR 0076) runs spawn-method worker processes from this same API process — plain picklable data in and out, worker touches no Redis/DB/sockets, SIGINT ignored in the child, kill-and-reap with a hang bound, closed in the lifespan `finally`. That is the pattern to extend, and it settles the PRD's "workers don't inherit live DB connections" question structurally: workers shouldn't have DB access *at all*; the coordinator keeps all persistence through the existing background loop and fence.
- **"No weaker admission" matches the admission contract verbatim.** `app/data_lake/admission.py:6` — "There is deliberately no positive cache: a refresh revokes admission even when the file's inode and timestamps have not changed yet." Keeping per-read admission while caching only the decode is the right split.
- **The bytes-in decode seam already exists.** `parse_day_zip` / `parse_history_zip` (`app/engine/data/lean_format.py:318`, `:459`) were split from `read_day` "precisely so a caller can hand in bytes it has already fingerprinted." The cache belongs exactly there, behind the check chain `ManifestBoundMinuteReader.read_day` already orders: `read_committed_bytes` → manifest digest → adjustment receipt → parse (`app/research/sweep/snapshot.py:241`).
- **The injectable engine seam makes the whole test strategy cheap** (see E below), and the same-resource serial control + "cache hits don't count as throughput" + the honest-failure clause are the right methodology.

## A. Decisions the PRD leaves open that block the vertical slice

### A1. The runtime's cgroup topology: "API and clerks outside that budget" is impossible for an in-API-process pool

The python-service container is 2 CPU / 2 GiB (`compose.yaml:272-277`), uvicorn is PID 1, and child processes share the parent's cgroup in Podman. So the "dedicated research runtime, eight cores / 16 GiB, API outside that budget" picture has exactly two topologies, and the PRD doesn't pick one:

- **(a) A separate research-runtime container.** Fits the fleet pattern (roles are already split: coordinator + clerk agents with their own limits). It needs a dispatch/progress transport hop, which the "no new broker/queue product" language should explicitly say it does *not* forbid (an internal service boundary is not Kafka).
- **(b) A child-process pool inside python-service with raised limits.** Then API + workers share one cgroup and one death: a pool memory bug OOM-kills the API — and in the default combined posture that same process *is* the clerk lane that stamps IBKR quote times and sends orders (`option_search.py:7-9` states this as the reason option searches left the process). That directly violates story 24 and "never let pressure kill the API." The 2026-09-05 cgroup kill recorded in ADR 0055's amendment is the measured precedent for what a pool bug does inside one cgroup.

Either is defensible; leaving it implicit is not. The chosen topology must also state: the `pids_limit` math (fleet postures cap the coordinator at 192 and agents at 256 — `compose.fleet.yaml:21`, `:71`; eight spawn workers importing the full app graph count against it), and the shutdown story (uvicorn's `--timeout-graceful-shutdown 10` will hard-kill workers mid-cold-import — which is *fine*, pending rows resume via Finish, but the ADR should say hard-kill-at-shutdown is the expected path rather than pretend a drain fits).

### A2. The run gate's maintainer rule, restated for processes — or the natural wiring wedges

`app/engine/run_gate.py:46` — "A thread holding the gate must never hand engine work to another thread and wait for it" — exists because that wedge is a C-level uninterruptible acquire no timeout rescues. Today the research path takes the *outer* gate hold at `execute_engine_backtest` (`app/services/engine_backtest_service.py:761`) and the inner hold in `BacktestEngine.run`.

In the pool world, an implementation that "keeps the outer hold for safety" while dispatching to workers has built the forbidden wedge in cross-process form (or, if the wait is interruptible, serialized the pool to one worker). The correct wiring is: **the parallel research path never enters `one_backtest_in_flight` in the parent at all**; the parent-process gate remains exactly for Strategy Lab and the sync endpoint (the PRD already keeps them serial); each worker's own process-local gate is its one-at-a-time guard, as the PRD says. Put the process-level restatement in the new ADR — "the coordinator never holds a gate while waiting on a worker" — because the current docstring's rule is thread-worded and a careful implementer could read it as permitting the parent hold. Note also `one_backtest_in_flight` refuses event-loop callers (`run_gate.py:141`), so dispatch stays on the coordinator's job thread.

`tests/engine/test_run_gate.py:325` ("an engine built anywhere still runs one at a time") will need deliberate reshaping to "…per process," not deletion.

### A3. Lease renewal must be bound to worker checkpoints, or ADR 0065's rejected failure mode returns

ADR 0065 D1 rejected the heartbeat thread because it "would keep proving liveness for a worker that has hung." The PRD's liveness section allows "queue waiting may renew only while an owned coordinator is actively checking cancellation and executable capacity" — but a coordinator *timer* renewing while eight workers are hung is precisely the heartbeat thread again, one level up: the study reads `running` forever, `job_is_live` never flips, and Finish is never offered.

Constructive fix — make one worker→coordinator **checkpoint stream** serve three obligations at once:

1. story 17's progress phases (queued / preparing / evaluating, with counts),
2. the lease-renewal proof: the coordinator renews only while at least one dispatched task checkpointed within its *declared per-phase wall bound* (or the coordinator itself made a state transition — dispatch, accept, persist),
3. cancellation observation for the 1-second dispatch-stop target.

Publishing the checkpoint contract (phases + bounds) in the ADR also defines "silent worker" detection below the lease TTL, which the PRD asks for but doesn't mechanism.

One adjacent invariant worth stating: the startup sweep (`app/jobs/progress.py:372`, ADR 0065 D2 "a restart takes every thread with it") stays true **only while the pool's lifetime is the API process's lifetime**. v1 keeps that (spawn children of PID 1 die with the container). Any future pool that survives coordinator restart silently invalidates D2 and must amend it first.

## B. The release targets have three unexamined eaters

### B1. Admission amplification: ~961 Postgres receipt round trips per full-window evaluation, ×8 workers — and the decoded-bar cache saves none of it

Per-read admission is `read_bytes` + sha256 + a Postgres receipt lookup per artifact (`admission.py:28-71` → `catalog_client.py` `has_committed_file_receipt`), plus the manifest digest compare (`snapshot.py:241`). The Oct-10 supplementary measurement says a warm 20-session admitted read took 0.063 s vs 0.030 s to parse the same bytes already in memory — i.e. **more than half of warm read cost is re-verification the PRD (correctly) refuses to remove**. A full-development evaluation admits 961 artifacts; at eight workers that is ~7,700 receipt round trips per evaluation-cycle against the one Postgres, alongside the coordinator's own reserve/complete writes.

The PRD records "database receipt calls" as a metric but has no concurrency story. Two concrete asks:

- **Batch the receipt lookup per (worker, window)**: one query fetching `(root, path, sha256, size, status)` for all artifacts of the evaluation's window, then verify each artifact in consumption order locally. Same checks, same refusals — revocation still bites because the row set is fetched fresh per evaluation — with ~3 orders of magnitude fewer round trips. This is probably the single cheapest lever for the 3× preparation-inclusive target, and it is semantics-preserving.
- **Publish a connection budget.** Catalog pools are per-loop `min 1 / max 10` (`catalog_client.py:41-52`). Eight workers each with their own loop is up to 80 connections before the API's own two pools, Backend, and clerks — against Postgres' default `max_connections=100`.

And one in-tree anti-pattern to name so nobody copies it: `LeanDailyDataReader._history_cache` (`lean_format.py:444-447`) returns cached bars on a hit *before* `_read_zip` runs (`:475-476`) — i.e. without re-running admission or the adjustment guard. It's benign today only because readers are per-run instances, but it is the nearest "decoded cache" precedent in the tree and it violates exactly the rule the new cache must keep. Fix it in this PR or record it as the named exception.

### B2. Cache key granularity decides whether the fold corpus ever warms

The PRD's identity list ("coverage including run-up") reads as **per-dataset** keying. The benchmark corpus and real studies are fold-heavy: 20 folds × distinct (train, test) windows plus the full-development window — per-window entries share almost nothing, so the 800 fold evaluations never warm and the preparation-inclusive target dies. **Per-artifact (per-day) entries** keyed by (root identity, content digest, adjustment version/basis, session policy, calendar identity, decoder version) compose into any window, and the run-up depth — planned once at lock for the whole legal search space (ADR 0074 §2) and therefore shared by every evaluation of the study — warms exactly once.

Ask: state that v1 cache identity is composable per artifact, or show the warm-hit arithmetic for the fold corpus under per-window keys. Related: **worker assignment determines warmness** — a cache-oblivious fair queue makes all eight workers decode every window. Either add warm-affinity dispatch (prefer the worker already holding the dataset, within fair share) or count that honestly in the preparation-inclusive numbers.

### B3. Single-study parallel width is structurally capped — publish it, and set expectations with it

Only batch units fan out: grid cells (`GRID_BATCH_SIZE = 50`, `app/research/golden_search/grid_procedure.py:58`), exam fits, recency runs, and the within-round Zoom sample (one ascending-value batch, `zoom.py:408`). Everything else is a dependency chain: Zoom's next round needs the previous winner (`zoom.py:390`), fold k+1 waits on fold k (kept sequential in v1, correctly), pair audits and evidence neighborhoods wait on winners. For the owner's actual workload — Zoom over three importance-ordered knobs — a round may be only a dozen points, and effective single-study parallelism sits well under eight for much of the study.

Asks: publish the frozen benchmark study's points-per-round distribution (the ≥2× end-to-end protocol target depends on it far more than on worker count); and make the progress UX say "waiting on adaptive dependency" rather than imply idle capacity is a defect. The second throughput eater in the same target: 800 of the 1,000 benchmark evaluations are ~0.75 s fold runs, so 50–100 ms of dispatch + IPC + persist per task is 7–13 % before any contention — report per-task transport cost as a first-class qualification number.

For orientation, sanity math on the PRD's own observations (not a benchmark): warm serial full-window ≈ 6.59 − 2.03 ≈ 4.6 s, so eight ideal workers cap near ~11× — roughly 2.7× headroom above the 4× target. That headroom is exactly what B1–B3 eat; it is adequate, but only if B1's batching happens.

## C. Correctness traps, each with the fix

**C1 — the exam's chance evidence reads detail runs from an in-memory dict a pool empties.** `StudyEvaluator._details` / `_warmups` (`evaluator.py:332-334`); `detail_of` (`:428`) only serves runs this evaluator instance executed; the exam's chance/drawdown readings resample those detail payloads "without an engine evaluation or budget" (`stages.py:678-690`). In a pool, detail runs execute in workers, the coordinator's dict never sees them, and chance readings silently read *missing* — which ADR 0074 D12 converts into wrongly-required research-weakness acknowledgements. Fix: `detail_of` becomes a repository read (`detail_json` is already persisted on every evaluation row). The PRD's "reuse existing summary projections" should name this seam.

**C2 — cross-process determinism environment.** The engine path is genuinely clean (verified: no global RNG, decimal-context, warnings, or pandas state anywhere under `app/engine` / `app/data_lake`), and ADR 0072's equal-inputs-equal-hashes property is real. But all its tests run in one process, and spawn workers are fresh interpreters: `PYTHONHASHSEED` differs per process, and any set/hash-of-strings iteration feeding accumulation or ordering can make two workers disagree invisibly. Cheap insurance: pin `PYTHONHASHSEED` and the native-thread envs (`OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=1`, bounded Arrow IO threads) in the worker initializer — which also makes "process count is the effective concurrency control" concrete instead of aspirational — and add one cross-process parity test (same evaluation in two workers + the coordinator → identical `result_hash` / `trade_log_hash` / `metrics_hash`) to the seam table.

**C3 — in-flight dedup must key on the full evaluation key, never the point hash.** `evaluation_key` includes window, scenario, *and the detail flag* (`evaluator.py:139`): a summary run and a detail run of the same point are distinct rows and distinct budget units. The PRD's "a duplicate request for an in-flight identity joins that task" is correct only if "identity" means the full key; a helpful implementation joining on `point_hash` will downgrade detail to summary or double-count budget.

**C4 — carry code identity into the task; don't re-derive it.** `context_digest` deliberately excludes volatile git fields so identical trees hash identically (`evaluator.py:78`, `:133-136`), and `resolve_code_identity` reads sources+env per process. The coordinator computes identity for reservation; workers should receive it in the task and *refuse on mismatch* with their own resolution — belt-and-braces against partial upgrades and dev-reload weirdness, and cheaper to diagnose than divergent-identity cache misses.

**C5 — keep all persistence in the coordinator.** `run_sync` blocks up to `DB_CALL_TIMEOUT_SECONDS = 60` and `CallerStoppedWaitingError` means "the work is still going, you just stopped waiting" (`app/utils/background_loop.py:26-35`). That semantic is manageable for the coordinator's own writes; give workers DB access and it reappears in a place nobody is waiting, plus N new pools and duplicate schema-ensure machinery. The option_search shape (workers pure, parent persists) preserves the fence, identity, and lease code untouched.

## D. ADR and authority-map debt (cheap, must-do in the same PRs)

- **ADR 0055's 2026-09-05 amendment** ("one backtest in flight") is the standing decision this executor supersedes. Amend it explicitly — citing this PRD — or the tree carries two conflicting authorities for the same question.
- **ADR 0065 needs an amendment** for coordinator-held leases over pool workers: D1's "the worker renews with every emit" becomes "the coordinator renews on worker checkpoints" (A3's contract). The startup-sweep assumption (D2) should get its one-sentence pool invariant too.
- **ADR 0049 D3a**: partial-coverage acceptance is owned by "the run-materialization seam." In the pool that seam executes inside workers; one sentence in the new execution-topology ADR keeps the ownership chain intact.
- The **engine authority map** row for the research runtime should carry A2's restated gate rule, not just the topology.

## E. Testing: the highest-leverage move the PRD underuses

The entire Golden Search semantic suite — cancel mid-evaluation, crash between reservation and result, superseded attempts, late completion, retry allowance, exactly-once counting, the lock→approve lifecycle (`tests/research/golden_search/test_study_service.py`, 15+ scenarios) — runs against an **injected engine executor**. Ship the pool as one more `ExecuteBacktest` behind the existing `engine_executor` seam (`evaluator.py:277-299`) and every one of those tests becomes a pool test with a fixture swap: a `FakeEngine` twin for quick CI, a real-worker-pool variant for the daily tier. The PRD's seam table lists the right *outcomes*; this is the *mechanism* that makes them affordable.

Additions the current suite genuinely lacks (verified — nothing in the tree uses `multiprocessing` today):

- **Completion-permutation tests**: scrambled completion order must yield identical recorded outcomes, budget trajectory, and winner. Selection is already order-invariant (`test_selection.py:51`) but nothing lands results out of order, and the cache-hit/new-run accounting assumes serial accumulation (`evaluator.py:391`, `:425`).
- **A real worker-death harness**: spawn an isolated worker against ephemeral Postgres + seeded lake, SIGKILL at a chosen seam — model it on the `FakeEngine`/`SimulatedCrash` pattern (`tests/_helpers/golden_search_study.py:124-151`), which already encodes "process death is a `BaseException` nothing may catch."
- **Budget-exhaustion × worker-loss**: the same recorded set as serial-with-a-failed-point. The prefix rule covers clean exhaustion; the interaction with a lost task near exhaustion is the one combination both features touch.
- Keep every real-process test `slow`-marked: the 16 CI shards are duration-balanced to 120 s (`scripts/run_fast_tests.py`, `pr_shard_durations.json`), and the PRD's "no new CI jobs" promise depends on it.

## F. Small but real

- **Pair audits are prefix-shaped too**: `_pair_maps` builds its cells "cell-by-cell so budget exhaustion keeps earlier cells" (`stages.py:302-319`). The parallel executor must treat it as a prefix batch like any other, or truncation semantics change silently.
- **Redis/SSE emit volume multiplies** with task-phase granularity (stream maxlen 50 k, `progress.py:54`; lease renewal rides emits). Bound emits per evaluation or chatter scales with worker count.
- **The pool must refuse to start under `uvicorn --reload`**, not warn: reload is opt-in dev (`compose.yaml:233`) but its own comment (`:226-232`) already documents it orphaning job workers.
- **The repo's own cautionary tale about concurrency serialization** is known-gaps F13 — a live, measured case of concurrent reads serializing on a shared loop with ~5-8× total-work inflation. Keep worker admission off any shared loop (each worker process owns its own, which the PRD's "workers initialize their own resources" already implies — worth saying as a rule).

---

None of this argues against building it. `run_gate.py` and `sweep/execution.py` predicted this exact future — "more throughput would take a process pool, which re-loads the bars in every process and so costs memory first" — and the decoded-bar cache is the answer to the half of that sentence everyone forgets. The four decisions that must be made explicit before the vertical slice are **A1 (topology), A2 (gate rule), A3 (lease checkpoints), and B2 (cache key granularity)**; each one gets decided by an implementation accident if the PRD doesn't decide it first.
