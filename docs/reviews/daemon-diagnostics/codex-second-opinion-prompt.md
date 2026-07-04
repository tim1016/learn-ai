# Second opinion request — Live host-daemon diagnostics catalog

You are reviewing a **design**, not writing code. Do not edit files. Read the
referenced code, then critique and extend the failure-scenario catalog below.

## Background

`learn-ai` runs a **host live-run daemon** (`PythonDataService/app/engine/live/host_daemon.py`)
on the operator's OS (Windows/Mac/Linux), *outside* the podman containers,
because IBKR error 420 rejects `reqRealTimeBars` when the API client's source IP
differs from the Gateway login IP (ADR 0003). The daemon owns: deploy/start/stop
of live-bot subprocesses, the in-memory process registry (the authoritative live
`strategy_instance_id → run_id` binding), git code-freshness, a 1 Hz control-plane
lease, orphan classification at boot, and an `lsof` Gateway-socket probe. The
containerized data plane (`polygon-data-service`) reaches it only via an
authenticated proxy (`X-Live-Runner-Token`, ADR 0007); **the browser never holds
the daemon token and never calls the daemon directly.**

We are designing a **diagnostics system** whose north star is: **pinpoint why a
specific bot is failing in the live daemon.** Not a flat global "is the daemon up"
report — a per-`strategy_instance_id` diagnostic ladder that surfaces the *first
failing rung* as the cause, plus a global report for control-plane-wide faults.

## Design already decided (do NOT relitigate unless you find a hard blocker)

1. **One composed authority.** A single data-plane builder authors the report. It
   *authors* the plumbing checks and *embeds by reference* the broker session
   mirror's already-authored socket-reconciliation attention codes (it never
   re-runs `lsof` or re-classifies a client). Exposed at its own snapshot endpoint
   AND embedded as a control-plane header in the existing broker session mirror
   page (`/api/broker/session-mirror`, ADR 0018).
2. **Compose from existing reads** — `host_daemon_client.fetch_health` (code /
   lease / boot / orphan-count) + the mirror snapshot (registry + sockets +
   reconciliation). No new daemon endpoint unless a check needs a fact `/health`
   lacks (extend `HostRunnerHealth` additively).
3. **Check model** — a new `DaemonDiagnosticCheck` superset (NOT the broker
   `DiagnosticCheck`): `check_id` (stable key, never rendered), `category`,
   `status` (`pass|warn|fail|skip`), trader `title`/`summary`, expandable
   `technical_detail` + structured `evidence`, `remediation`, `scope`
   (`GLOBAL|ACCOUNT|INSTANCE|RUN`), optional `action`.
4. **No bare "degraded."** Every distinct cause is its own named check. The report
   surfaces a closed `dominant_condition` enum + backend-authored `headline`
   (title/summary/remediation); `pass|warn|fail` is only the colour.
5. **Always HTTP 200** with a full report even when the daemon is down (failure
   lives in the checks, not the status code); a top-level `transport` field mirrors
   `DaemonResult.kind` (`CONNECTED|UNREACHABLE|AUTH_FAILED|PROTOCOL_ERROR|INCOMPATIBLE_CONTRACT`).
6. **Container-actuatable gate.** A fix is an invocable button ONLY if the data
   plane can cause it from inside the container. v1's only such action is
   `renew_lease` (existing `POST /api/live-instances/daemon-health/renew-lease`).
   Host-level fixes (start/restart daemon) are NEVER buttons — only honest,
   **platform-aware** guidance (systemd/launchd/NSSM per the daemon's reported OS).
7. **Backend-authored redaction.** Host-absolute paths reduced to repo-relative
   with home/hostname stripped; no tokens/argv; operator handles pass through; no
   frontend exposure gate.

## Files to read before answering

- `PythonDataService/app/engine/live/host_daemon.py` (daemon: registry, health,
  deploy/start/stop, `_exit_reason_from_code`, `validate_ibkr_host_allowed`,
  crash-retired recovery, `emergency_flatten`)
- `PythonDataService/app/engine/live/host_daemon_client.py` (`DaemonResult` kinds,
  timeouts, `fetch_health`, `fetch_gateway_sockets`)
- `PythonDataService/app/engine/live/daemon_transport.py` (`DaemonResult`)
- `PythonDataService/app/engine/live/daemon_connectivity_monitor.py`
- `PythonDataService/app/schemas/live_runs.py` (`HostRunnerHealth`,
  `HostRunnerProcessStatus`, `HostRunnerInstance`, exit codes)
- `PythonDataService/app/schemas/broker_session.py` (`BrokerSessionAttentionCode`,
  identity/recency types, `GatewaySocketRow`)
- `PythonDataService/app/routers/live_instances.py` (`/daemon-health`,
  runtime-freshness, `_visible_live_run_dir`, engine_runtime handling)
- `PythonDataService/app/broker/ibkr/diagnostics.py` + `models.py`
  (`DiagnosticCheck` precedent; `IbkrConnectionHealth` connection-state machine)
- `docs/architecture/adrs/0003-*`, `0007-*`, `0011-*`, `0018-*`
- `CONTEXT.md` §§ "Daemon diagnostics — control-plane health", "Broker session
  mirror — client-connection observability"

## The failure-scenario catalog to critique (this is the ask)

Each row: scenario → operator symptom → repo data source → proposed
`dominant_condition` → remediation type (button / host-guidance / navigation).

### GLOBAL — control-plane faults that hit every bot at once
- **G1** Daemon process down / port unbound → deploy/start/stop all fail →
  `DaemonResult` UNREACHABLE (httpx ConnectError) → `UNREACHABLE` → host-guidance.
- **G2** Reachable but token rejected (stale/rotated `.host-daemon-token`, ADR
  0007) → 401/403 → `AUTH_FAILED` → host-guidance (restart both to re-sync).
- **G3** Reachable, auth ok, body unparseable / wrong schema (build mismatch) →
  pydantic `ValidationError` → PROTOCOL_ERROR/INCOMPATIBLE_CONTRACT →
  `BUILD_MISMATCH` → host-guidance (restart daemon).
- **G4** Daemon running stale code (does NOT reload on `git pull`) →
  `HostRunnerHealth.code_stale/commits_behind` → `STALE_CODE` → host-guidance.
- **G5** Control-plane lease stale/DRAINING → `lease_status` +
  `last_lease_written_at_ms` vs now → `LEASE_STALE` → **renew_lease (button)**.
- **G6** Daemon restarted → `daemon_boot_id` flip; in-memory registry wiped →
  amnesia risk → `BOOT_CHANGED` → navigation.
- **G7** `lsof` socket probe unavailable (missing/denied/timeout) →
  `BrokerSocketProbeError` / mirror ghost-detection status →
  `SOCKET_PROBE_UNAVAILABLE` → host-guidance.
- **G8** Requested IBKR host not in allowlist → start 400
  (`validate_ibkr_host_allowed`) → host-guidance/config.
- **G9** `control_plane/` dir unwritable → lease writer fails at startup (logged,
  tolerated) → `LEASE_UNWRITABLE` → host-guidance; no `renew_lease` button.
- **G10** `LIVE_RUNS_ROOT`/repo_root misconfig → host-vs-container path mismatch →
  bound run dir invisible to data plane → `RUN_DIR_INVISIBLE`.

### PER-BOT — registry / process (the pinpointing ladder core)
- **B1** Never started → no managed process; operator intent vs registry gap.
- **B2** Started but exited/crashed → `process.state=exited`, `exit_code`
  (1 fatal_halt / 2 operator_refusal / 3 exception / 4 hydration_failure;
  `_exit_reason_from_code`) → surface the exit reason as the cause.
- **B3** Running but registry forgot it (daemon restart amnesia) → mirror
  `REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE` → `REGISTRY_AMNESIA`.
- **B4** Start refused — account frozen / crash-retired recovery →
  409 `crash_retired_restart_blocking_binding`.
- **B5** Start refused — instance already active → 409.
- **B6** Start refused — all-in coexistence guard (sibling `SetHoldings(1.0)` same
  symbol) → 409.
- **B7** Start refused — deploy/sizing errors (`SizingPolicyMissingError`,
  `UnknownLiveConfigKeyError`, `DirtyTreeError`, `SpecOrAuditMissingError`,
  `InvalidInstanceIdError`, `StrategyInstanceIdAlreadyUsedError`).
- **B8** Deployed but not started (`DEPLOYED` lifecycle, no process).

### PER-BOT — broker socket (mirror-owned, embedded by reference)
- **S1** Process live but no IBKR socket (connect failed / error 420 source-IP) →
  mirror `STARTED_BUT_NO_SOCKET`.
- **S2** Orphaned bot socket (process dead, socket lingers; safety hazard) →
  `ORPHANED_BOT_SOCKET`.
- **S3** Socket without live PID (half-open) → `SOCKET_WITHOUT_LIVE_PID`.
- **S4** clientId collision → `IbkrClientIdInUseError`.
- **S5** Ghost/foreign socket (manual TWS login) → `GHOST_SOCKET`.
- **S6** `client_id → strategy_instance_id` join unavailable
  (`engine_runtime.json` has `client_id: null`) → `CLIENT_SIGNAL_STALE`.
- **S7** Broker link degraded/lost per client (1100/1101/2103/2105) →
  `IbkrConnectionHealth.connection_state` (broker health, not daemon).

### PER-BOT — child runtime / artifacts
- **R1** Child `engine_runtime.json` stale / frozen `"connected"` for days →
  runtime freshness (`evaluate_runtime_freshness`, `ENGINE_RUNTIME_MISSING/INVALID`).
- **R2** Bound run dir not visible to data plane (host vs container path) →
  `_visible_live_run_dir` returns None → durable-only.
- **R3** Ledger/WAL corrupt → `IntentWalCorruptError` (self-poison risk).
- **R4** Readiness sidecar missing/malformed.

## Proposed per-bot ladder (first failing rung = the answer)

1. Daemon reachable/auth/contract (G1–G3) — if down, **all** bots undiagnosable.
2. Bot has a managed process? (B1)
3. Running vs exited-and-why? (B2)
4. Registry consistent, or amnesia? (B3)
5. Has an IBKR socket? (S1)
6. Socket attributable/healthy — not orphan/ghost/collision? (S2–S6)
7. Child runtime fresh? (R1)
8. Code fresh / artifacts visible? (G4, R2, R3)

## Questions for you (Codex)

1. **Completeness** — what failure scenarios are MISSING? Especially daemon-side
   ones you can find in the code that I have not listed.
2. **Correctness** — is any scenario mis-sourced, i.e. the data source I named
   does not actually exist or does not carry what I claim? Verify against the code.
3. **Undiagnosable-from-available-data** — which scenarios CANNOT be pinpointed
   from `fetch_health` + the mirror snapshot alone, and would force a new daemon
   fact/endpoint? (I want to know what the "compose from existing reads" decision
   actually can't cover.)
4. **Ladder ordering** — is the first-failing-rung order right? Any rung that
   should short-circuit differently, or any two rungs that should merge/split
   (e.g. `BOOT_CHANGED` vs `REGISTRY_AMNESIA`)?
5. **v1 vs deferred** — which rungs are essential for a first shippable slice vs
   deferrable (my current lean: defer a logs/incidents check and deep per-instance
   managed-runner crash detail, link out instead)?
6. **Global vs per-instance shape** — should the per-instance ladder be a separate
   endpoint (`GET /api/live-instances/{sid}/daemon-diagnose`) or a per-instance
   sub-report inside one global report? Trade-offs?
7. **Overlap risk** — anywhere this duplicates the broker session mirror (ADR
   0018), the bot cockpit readiness vector, or `IbkrConnectionHealth` in a way
   that would create a second authority for the same fact.

Answer concretely and cite the file/symbol you verified each point against.
