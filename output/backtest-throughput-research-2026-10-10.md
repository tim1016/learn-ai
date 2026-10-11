# Faster backtests on the research machine

Research dated October 10, 2026. **Build reusable historical datasets and a dedicated pool of backtest processes before migrating to TimescaleDB.** The present workload repeats expensive data preparation and deliberately executes evaluations sequentially. The machine has substantial unused capacity available to research. TimescaleDB could eventually serve shared historical queries and ingestion, but it would not remove either execution constraint.

This is a local research artifact, not an accepted architecture decision or a file to merge into master. The investigation used source inspection, read-only runtime queries, and short unsaved replays; it changed no production source, resource limits, or active study state.

**Evidence from the current workload**

The observed Golden Search runs `ema_crossover_signal` on GLD, with a 5,000-evaluation budget, a development interval from September 2022 through June 2026, and 20 folds using six months of training followed by two months of testing. Its summary initially showed 979 consumed evaluations and 646 cache hits. These are moving observations, not final totals. Completed evaluation reuse already exists; adding a generic result cache would duplicate an existing feature.

| Resource | Observed configuration or size |
|---|---:|
| Mac memory | 128 GiB |
| Mac logical CPUs | 18 |
| Podman virtual machine | 16 virtual CPUs, 32 GiB |
| Backtest/API container | 2 CPUs, 2 GiB |
| Local mounted data lake | About 177 MB, as reported by filesystem usage |
| PostgreSQL database | About 95 MB |
| Data lake catalog rows | About 12,605 estimated live rows |

The host and container values came from runtime inspection; the service limits also appear in [compose.yaml](/Users/inkant/learn-ai/compose.yaml:274). These sizes describe the current local installation, not a future options-chain or tick-history collection.

The engine has a process-wide [one-backtest lock](/Users/inkant/learn-ai/PythonDataService/app/engine/run_gate.py:114). Golden Search [evaluates points sequentially](/Users/inkant/learn-ai/PythonDataService/app/research/golden_search/evaluator.py:341) and [runs validation folds sequentially](/Users/inkant/learn-ai/PythonDataService/app/research/golden_search/stages.py:415). Jobs currently execute in [daemon threads within the API service](/Users/inkant/learn-ai/PythonDataService/app/jobs/runner.py:44). Raising CPU limits alone therefore does not expose all those cores to independent backtests.

Earlier measurements recorded in the [sweep runner](/Users/inkant/learn-ai/PythonDataService/app/research/sweep/execution.py:1) found four cells took 24.5 seconds with one thread, 27.9 with two, and 38 with four. Those are historical measurements, not a fresh benchmark. They explain the current guard: this Python engine gains little from more threads while concurrent runs consume more memory.

The data path is also doing repeated work. For each evaluation, a new [manifest-bound reader](/Users/inkant/learn-ai/PythonDataService/app/research/sweep/snapshot.py:221) opens daily ZIPs, verifies hashes and adjustment evidence, and parses CSV values into immutable bars with Decimal prices. Each day's [committed admission](/Users/inkant/learn-ai/PythonDataService/app/data_lake/admission.py:28) performs a PostgreSQL receipt lookup. These are required integrity checks, not accidental redundant code.

**Measured contribution of data preparation**

Two short replays used the observed study's stored points, windows, execution assumptions, and receipted data manifest. They ran in a separate interpreter using the currently mounted source, with automatic fetching and persistence disabled. Lightweight timers surrounded existing functions. The active study continued running, and its imported code need not be identical to the current checkout, so these are representative measurements rather than a formal reproduction of its sealed code identity.

| Measurement | Six-month validation evaluation | Full development evaluation |
|---|---:|---:|
| Total elapsed time | 0.752 s | 6.593 s |
| Read, admit, verify, and decode daily data | 0.426 s | 3.736 s |
| ZIP/CSV parsing within that read time | 0.226 s | 2.025 s |
| Committed-byte admission within that read time | 0.129 s | 1.083 s |
| Result aggregation | 0.061 s | 0.702 s |
| Daily artifacts read | 125 | 961 |
| Evaluated minute bars | 47,769 | 372,282 |
| Trades | 0 | 29 |

The parsing and admission rows are nested within total read time; do not add them again. Admission includes file access, path/root work, hashing, the thread-to-event-loop bridge, and the database round trip. It is not a measurement of SQL execution alone. Input reads also include warmup, explaining the difference between days read and scored bars.

Both samples spent about **57% of elapsed time preparing data**. A supplementary 20-session warm-cache sample took a median 0.063 seconds through the admitted reader versus 0.030 seconds parsing ZIP bytes already in memory. This distinguishes repeated validation/file overhead from parsing overhead; it does not justify bypassing validation.

Observed production records corroborated the overall durations: at one observation, 44 completed search evaluations had a median recorded duration of 6.538 seconds; 895 completed validation evaluations had a median of 0.813 seconds. Recorded durations span reservation to completion and can include orchestration, so they are not pure engine timings. The two instrumented replays reported a cumulative interpreter peak of about 438 MiB after the longer replay. That is neither private memory per future worker nor a concurrency sizing guarantee.

These observations justify investigating prepared-data reuse. They do not establish a speedup from Parquet, TimescaleDB, or eight workers, because those alternatives have not yet been benchmarked here.

**Recommended engineering sequence**

1. **Separate research execution from the API and give it a bounded process pool.** Start with four persistent workers, then measure eight and twelve. Keep the coordinator responsible for deterministic reservations, result recording, cancellation, attempts, and leases; workers perform the simulations and return bounded evidence. Python 3.12 processes bypass the GIL, and worker initialization can load reusable inputs once. Send dataset identifiers and parameter sets rather than large bar lists on every submission. [Python process executors](https://docs.python.org/3.12/library/concurrent.futures.html#processpoolexecutor)

2. **Reuse prepared data across evaluations.** First prototype a bounded cache of parsed immutable bars, keyed by exact source digest, adjustment evidence, reader version, and session policy, while preserving the existing admission rules. Then establish an immutable study dataset that amortizes validation and decoding across the whole study. The cache must include calendar identity, warmup coverage, provider, resolution, and adjustment basis. Each simulation retains its own portfolio, orders, indicators, and other mutable state.

3. **Use the available hardware deliberately.** A reasonable target to benchmark is a 16-vCPU/64-GiB Podman VM and a dedicated research pool with an 8-CPU/16-GiB allowance. This is a proposed budget, not a measured requirement or an immediate configuration change. The current 32-GiB VM may already be sufficient for initial tests. Keep resources available for the API, PostgreSQL, and live clerks; increase the research budget if measured throughput improves. Evaluate twelve workers only after the eight-worker result, since logical CPUs are not necessarily equal in sustained performance.

4. **Add a compact storage representation where it earns its place.** Use versioned Parquet for durable prepared datasets and existing PyArrow for bulk reads. Benchmark a read-only Arrow IPC or NumPy memory-mapped representation for repeatedly accessed data. Parquet memory mapping still requires decoding; it does not automatically share decoded values among workers. Mapping Arrow/NumPy buffers also does not make the existing Decimal/TradeBar engine zero-copy: that adapter still needs a measured design. Preserve Decimal values or an exact fixed-point representation, including volume; do not silently switch the engine to floats. [Arrow Parquet](https://arrow.apache.org/docs/19.0/python/parquet.html#reading-parquet-and-memory-mapping), [Arrow IPC](https://arrow.apache.org/docs/19.0/python/ipc.html), [NumPy memory maps](https://numpy.org/doc/2.2/reference/generated/numpy.memmap.html)

5. **Optimize the remaining engine work after measuring again.** The profile also identified bar consolidation and timestamp conversions as meaningful work. Reuse canonical session schedules and identical indicator inputs where valid. Summary evaluations already omit response/chart copies, but the engine still builds a minute equity curve internally; reducing that memory requires preserving the exact statistics and accumulation semantics. A daily-only curve is not an interchangeable substitute. [Engine equity sampling](/Users/inkant/learn-ai/PythonDataService/app/engine/engine.py:452), [summary response path](/Users/inkant/learn-ai/PythonDataService/app/services/engine_backtest_service.py:1103)

Adaptive search has dependencies. Points within an already-chosen round can run concurrently; the next round still depends on the previous round's results. Independent studies and folds offer additional parallel work, but finite budgets must be allocated deterministically. The first implementation should retain input ordering and tie-breaking, prevent duplicate pending evaluations, and preserve final-test isolation, cancellation, and recovery. Adding workers must not change which candidates get tested when the budget runs out.

There is one substantive architecture decision before admitting a dataset only once: **what happens when source admission or corporate-action evidence is revoked?** Today's reader intentionally refuses stale or uncommitted bytes on every read. A cache cannot silently relax that rule. Record whether a pinned research snapshot may finish on its recorded revision or must stop on revocation, and implement the corresponding validity checks. Until that decision is accepted, cache parsing while retaining required revalidation. This follows the existing [snapshot contract](/Users/inkant/learn-ai/PythonDataService/app/research/sweep/snapshot.py:1) and [admission contract](/Users/inkant/learn-ai/PythonDataService/app/data_lake/admission.py:1).

**Where TimescaleDB fits**

| Option | Benefit for this workload | Recommendation |
|---|---|---|
| Existing PostgreSQL plus prepared local datasets | Keeps the current catalog/results store; removes repeated preparation and enables local processes | First investment |
| Parquet with PyArrow | Bulk columnar reads and portable versioned datasets; PyArrow is already installed | Prototype as the prepared-data layer |
| DuckDB over Parquet | Convenient analytical selection with column/filter pushdown | Optional; add only for a demonstrated query benefit |
| TimescaleDB | Time-partitioned historical queries, ingestion, columnstore, retention, and rollups | Benchmark when those become measured needs |
| Multiple machines and a distributed scheduler | More compute after the single-machine pool saturates | Later; the current machine is not yet well utilized |

Timescale hypertables partition time-series data into chunks. Their usefulness depends on access patterns, indexes, and chunk sizing. The documented BIGINT epoch-millisecond support fits this repository's timestamp requirement; appropriate integer-time functions are needed for time-based policies. Columnstore organization can favor instrument-and-time queries. These are useful database capabilities, but they do not parallelize Python strategies or eliminate conversion to engine objects. [Hypertables](https://www.tigerdata.com/docs/learn/hypertables/understand-hypertables), [integer partitioning](https://www.tigerdata.com/docs/learn/hypertables/partitioning-hypertables), [storage architecture](https://www.tigerdata.com/docs/learn/deep-dive/whitepaper)

Continuous aggregates can accelerate repeated rollups. They are not a replacement for stateful strategy execution. In this repository, SQL-authored trading bars or indicators would also require an explicit canonical-math exception and parity proof; ordinary time buckets do not establish exchange-calendar equivalence. [Continuous aggregates](https://www.tigerdata.com/docs/learn/continuous-aggregates)

DuckDB's Parquet reader can push filters and column selection into scans, but a strategy requiring every bar in a window must still consume those bars. It is optional here because PyArrow and NumPy already exist. Avoid adding a query engine solely to run the same full scan repeatedly. [DuckDB Parquet](https://duckdb.org/docs/current/data/parquet/overview)

Timescale earns a migration proposal when realistic concurrent history extraction remains a major bottleneck after prepared-data reuse, or when ingestion/corrections/retention create a concrete operational need. Compare it with tuned ordinary PostgreSQL and local Parquet using identical datasets and full client extraction costs. A future broad options or tick workload could change that outcome; the current 177-MB lake does not establish such a need. Even then, a sensible design is Timescale feeding versioned local research snapshots, with Python workers computing from those snapshots.

**Acceptance criteria for the first implementation**

Use a fixed set of representative full-history, fold, and trade-heavy evaluations. Compare total valid evaluations per hour at 1, 2, 4, 8, and optionally 12 workers; separate cold preparation from warm repeated runs. Record stage times, queue wait, source query counts, CPU utilization, private and shared memory, and result-writing time. Benchmark host-mounted storage against VM-local derived caches if file access remains material; the current data lake is a host bind mount, but its contribution has not been isolated.

Require identical consumed bars, timestamps, Decimal values, signals, fills, trades, and pinned-tolerance statistics. Exercise cancellation, worker loss, retry fencing, finite-budget determinism, and snapshot revocation. Use the existing Python engine and golden fixtures as the authority. Keep IBKR's live market-data role, Alpaca order execution, and clerk stores outside this historical-research redesign.

The next deliverable should be a bounded process-worker and prepared-dataset prototype with a throughput comparison. That provides the evidence to select a permanent hardware allocation and determine whether any database project is warranted.
