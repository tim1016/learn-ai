> **Status:** Archived / superseded (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, ADR-0026, and `docs/architecture/engine-authority-map.md`.
> **Archived because:** This router-level audit records the retired cohort/singleton surface and is retained only as evidence.

# Live-control-plane concurrency audit

**Baseline:** `6ebc34d3c`; reviewed again after the #1125 diagnostics pilot. This audit covers the mutable router-level state in `app/routers/live_instances.py` under the configured production command: one uvicorn worker, one asyncio event loop, and FastAPI lifespan startup/shutdown. `compose.yaml` and `PythonDataService/Dockerfile` do not pass `--workers`.

## Result

No production-reachable first-call or read-during-swap race was confirmed in the current single-worker lifecycle. The apparent check-then-create helpers are synchronous between the check and assignment, so another coroutine cannot interleave there. The objects that perform asynchronous work own their own lifecycle/refresh guards. Consequently this PR does not add speculative locks.

There is one **latent** shutdown edge: `SurfaceHubRegistry.stop_all()` snapshots its dictionary, awaits the owned stops, then clears the dictionary. A caller that creates a new hub during that await would have its hub removed without being stopped. Uvicorn stops accepting and drains requests before it runs FastAPI lifespan shutdown, and the only normal caller of `stop_surface_hubs()` is that shutdown path, so it is not reachable in the deployed model. The planned lifespan-owned runtime must nevertheless close this edge by rejecting creation after it enters `stopping`.

## Singleton verdicts

| State | Creation / replacement | Await boundaries and concurrent readers | Verdict | Reason and follow-up |
|---|---|---|---|---|
| `_cohort_launch_locks` | `_cohort_launch_lock()` does synchronous `get` → `Lock()` → dictionary assignment; `launch_cohort()` then awaits under that returned lock. | No await occurs between lookup and assignment. Requests for one normalized account serialize at `async with`; different accounts deliberately do not. | **fixed (in-process)** | The existing per-account lock closes the admission interleaving it was introduced for. It is process-local and never evicted; multi-worker operation would need durable/distributed admission ownership and the future runtime should bound retirement of idle account keys. |
| `_SURFACE_HUBS` | `SurfaceHubRegistry.get_or_create()` is synchronous; `start_surface_hubs()` starts boot hubs and mutation paths call `_ensure_surface_hub_started()`. | Each `SurfaceHub` has lifecycle and refresh guards. Stream readers capture a hub; `stop()` sets the terminal event and closes watchers. | **accepted-with-reason** | Normal ASGI shutdown prevents a new HTTP reader while `stop_all()` awaits. The latent create-during-stop orphan described above remains a design debt, not a current live request race. Move this registry into `LiveInstanceSurfaceRuntime` with an explicit `running/stopping/stopped` state before supporting multi-worker or alternate lifecycle callers. |
| `_SURFACE_RUNS_CACHE` | Created at import; invalidated after mutation/deletion and on shutdown. | `VisibleRunsSnapshotCache.get()` double-checks under `_lock`; `invalidate()` uses the same lock. It explicitly coalesces concurrent run scans. | **fixed (internal lock)** | The cache already owns the only required async mutex. Callers may observe the old valid TTL snapshot during a mutation until its explicit invalidation runs, which is an intentional freshness contract rather than a lost-update race. |
| `_FLEET_DAEMON_PROVIDER` | Assigned before the first await in `start_surface_hubs()` and swapped to `None` during shutdown after hubs stop. | Provider lifecycle, refresh task, and poll task have their own guards. `_fetch_surface_instance_process()` captures the provider before awaiting; shutdown is sequenced after request draining. | **accepted-with-reason** | A second independent `start_surface_hubs()` caller could race on the provider's lifecycle, but only FastAPI lifespan invokes startup. The provider itself coalesces refreshes. The future runtime must make start/stop idempotence an explicit API rather than relying on the router call graph. |
| `_FLEET_ROSTER_HUB` | `_fleet_roster_hub_for()` synchronously checks and assigns; `stop_surface_hubs()` swaps it to `None`, then stops the captured hub. | SSE readers capture the hub before yielding. A late `subscribe()` after `stop()` receives a terminal queue; readers do not resurrect the producer. | **benign in the deployed lifecycle** | No await lies between the lazy-init check and assignment. The shutdown capture/swap is safe for existing stream readers. As with `_SURFACE_HUBS`, a future explicit runtime should reject creation once stopping starts. |

## Reachability evidence

- `start_surface_hubs()` is invoked once from `app.main`'s lifespan before normal requests are served.
- `stop_surface_hubs()` is invoked from the matching `finally`, after the cohort scheduler and evidence sampler stop.
- The health/status routes do not call start or stop. Mutations only refresh an already process-local runtime.
- `SurfaceHub`, `FleetDaemonSnapshotProvider`, and `VisibleRunsSnapshotCache` each use their own `asyncio.Lock`-protected lifecycle or refresh coalescing.

## Regression posture

The existing surface tests already pin the relevant behavior: concurrent refresh coalescing, stop/restart generation fencing, terminal subscriptions, shutdown ordering, fleet-provider sharing, and mutation refresh. No new race test is appropriate without an interleaving that is reachable in the actual lifespan model. A synthetic test that calls private startup/shutdown concurrently would prove only an unsupported lifecycle invocation and could incorrectly force a lock whose semantics mask the real ownership issue.

The next extraction must add a `LiveInstanceSurfaceRuntime` constructed in FastAPI lifespan, install it on `app.state`, and expose only `start()`, `stop()`, `hub_for()`, and `refresh_after_mutation()` methods. Its `stopping` state must make the latent registry edge impossible by construction.
