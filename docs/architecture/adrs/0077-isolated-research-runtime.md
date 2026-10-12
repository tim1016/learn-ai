# ADR 0077: An isolated research runtime owns research job execution

- Status: Accepted
- Date: 2026-10-11
- Owners: @tim1016
- References: PRD #3034 (faster backtest research), #3042 (per-runtime job
  ownership, schema v17), #3040 (ordered ready-batch seam), ADR 0065 D2
  (amended: ownership per runtime), ADR 0055 (Python-owned research tables
  and its one-backtest amendment), ADR 0062 D1 (the control-plane precedent
  for one process, one authority, own volume), ADR 0068 (Python owns the
  canonical math — unchanged by this ADR).

## Context

PRD #3034's machine cannot serve research throughput from the API container:
its 2-CPU/2-GiB cgroup is sized for serving, and the one-backtest-in-flight
gate (ADR 0055's 2026-09-05 amendment) is a memory constraint of that
container, not an authority boundary. The PRD decides research execution
moves to a separate container with its own cgroup — eight CPUs, 16 GiB and a
512-PID cap, the eight-worker qualification envelope — while study
protocols, evaluation identity and the Python-owned persistence stay
authoritative and unchanged. #3042 already made job ownership per runtime:
a singleton coordinator holds a session advisory lock, dispatches are
stamped with its boot generation, and the API's startup sweep skips a live
coordinator's jobs.

**Amendment 2026-10-12 (#3049-#3053):** the mounted dispatch surface now
carries all four research callers — the Golden Search stage, Grid Search,
the Walk-Forward Study and Recency — each with its own persistence
contract intact and every scientific identity unchanged; the pooled
widths, fair scheduling and progress contract are #3046-#3052's, and the
operator's executor selection, drain and rollback procedure is
`docs/runbooks/research-runtime-capacity.md`. Decision 1's "the internal
research dispatch routes" reads with that surface; the concurrency
bounded by the envelope is qualified per PRD #3034's evidence, not
assumed.

## Decision

1. **One research runtime process owns execution; the API forwards.** A
   minimal ASGI application — `app.research_runtime:app`, the shape of the
   LEAN sidecar launcher, not of `app.main` — mounts only the internal
   research dispatch routes and starts only research resources: the
   coordinator authority claim, the research schema and the shared job
   store. It imports no broker, clerk, provider-subscription or fleet
   startup graph. The API keeps its HTTP contracts unchanged; when
   `RESEARCH_EXECUTION_MODE=research` it hands each dispatch to the runtime
   over the internal boundary (private-target-enforced client,
   `X-Data-Plane-Control-Secret`), and never binds, stamps or starts a
   thread itself — binding and ownership happen once, in the runtime.
2. **Default routing stays local; selection is opt-in.** The mode defaults
   to `local`: every research job runs exactly as before, on the API
   process's own threads, claiming the coordinator authority as it did
   since #3042. The runtime container ships behind the `research` compose
   profile (distinct cgroup: 8 CPUs / 16 GiB / 512 PIDs, never under the
   API's 2-GiB limit), and an operator selects it per deployment with the
   profile plus the mode env. Both runtimes executing is what the singleton
   ownership lock refuses; in forwarding mode the API claims no authority.
3. **A transport timeout is not a refusal.** A forward that times out is
   reconciled against the durable study binding: a row already bound to the
   same job id means the original acceptance stands and the retry joins it
   (the runtime's lease is the liveness authority); anything else refuses
   with the transport reason. Refusals from the runtime — token mismatch,
   a stage another job holds, a closed redelivery — pass through with their
   status and reason.
4. **The runtime's lease contract is the checkpoint contract (#3041).** In
   the runtime, cancellation polls renew nothing; the lease renews only on
   accepted checkpoints — the stage's phase transitions and durable
   evaluation completions — fenced by the coordinator's boot generation.
   The API's local path keeps the historical emit-and-check renewal.
5. **Restart semantics are the ownership semantics (#3042).** An API-only
   restart cannot close the runtime's jobs: it claims no authority and its
   sweep skips the live generation's stamped dispatches. A research runtime
   restart releases the lock (or dies with its connection); its jobs read
   interrupted and Finish re-runs the pending prefix reusing completed
   evaluations. Reload plus research execution — either process — is a
   startup error: a reloader restarting mid-evaluation under a held
   advisory lock is the corruption the fence exists to prevent.
6. **This slice changes no science.** The runtime runs the existing serial
   engine and study modules unchanged; evaluation identity, the attempt
   fence and every recorded result are byte-identical. The bounded-worker
   boundary inside this container is #3044's, not this ADR's.

7. **The runtime's evaluations run in one spawned worker (#3044).** A
   bare child interpreter — `python -m app.research.golden_search.worker`,
   born with `PYTHONHASHSEED=0` and the native thread counts pinned before
   interpreter startup — speaks one JSON-line protocol over stdin/stdout and
   owns computation only: no database, Redis, provider or broker handle
   exists in it. Every artifact read asks the runtime for the current
   committed-byte verdict (a fresh verdict per read; nothing survives an
   individual read), then runs the manifest/adjustment checks and the decode
   against its own process-local cache (#3038) — the same
   `execute_engine_backtest`, gates and all, in the child where they are
   process-local. The runtime owns the child from spawn through exit:
   bounded readiness, one task lane, admission verdicts on its own bounded
   I/O, checkpoints feeding the job's lease gate, kill-and-reap on silence,
   loss or shutdown. A lost or wedged child interrupts the stage — the
   affected reservations stay pending for Finish; bounded retry is #3045's.
   One worker, still serial: concurrency is later slices' qualification,
   never this ADR's.

## Consequences

- The engine authority map gains the research-runtime row; the Golden
  Search row's entry point names the runtime when the mode selects it.
- ADR 0055's one-backtest amendment now describes the API container's
  constraint; the runtime's envelope is the new container's, and the gate
  itself is unchanged inside each process.
- The runtime counts one dedicated advisory-lock connection inside the
  research runtime's coordination/persistence budget (PRD #3034's 12); the
  benchmark's connection inventory records it when the stacks meet.
- Combined API/clerk deployments are unaffected: the runtime is a separate
  service definition, opt-in via profile, and never placed under the API's
  limits.

## Considered alternatives

- **.NET routing dispatches directly to the runtime** (a per-type base URL
  beside `PolygonService:BaseUrl`): rejected for this slice — the PRD names
  the API as the forwarder, .NET stays the transport that mints job ids,
  and the API's reconciliation can read the durable binding directly.
- **Redis-mediated dispatch (a stream the runtime consumes):** more moving
  parts; progress, results and cancellation already ride the shared job
  store from whichever process owns execution, so the HTTP hop is the
  smaller seam.
- **In-API child process pool with raised limits:** explicitly rejected by
  the PRD — an in-API pool does not satisfy the separate-container decision.
