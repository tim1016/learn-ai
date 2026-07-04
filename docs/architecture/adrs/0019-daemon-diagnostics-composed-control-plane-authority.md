# ADR 0019 — Live host-daemon diagnostics are a composed control-plane authority in the data plane, not a daemon-side `/diagnose`

**Status:** Proposed 2026-07-04. Drafted during the 2026-07-04 `grill-with-docs` session ("pinpoint why a specific bot is failing in the live daemon"), with a code-verified Codex second opinion integrated (prompt at `docs/reviews/daemon-diagnostics/codex-second-opinion-prompt.md`). Full design at `docs/architecture/daemon-diagnostics-proposal.md`; vocabulary in `CONTEXT.md` § "Daemon diagnostics — control-plane health".

**Decision drivers:** The host live-run daemon (`app/engine/live/host_daemon.py`) is the sole authority for whether a live bot is running, yet the operator has no structured surface that answers *why a specific bot is failing*. Today's daemon story is a flat `HostRunnerHealth` envelope + a folded `DaemonConnectivityMonitor` state; the only interpreted self-test in the codebase is `/api/broker/diagnose`, which tests the data-plane's *own* IBKR client, not the daemon hop. ADR 0007 explicitly left "a `/diagnose`-style self-test for the daemon hop" as an unbuilt follow-up. Meanwhile the operator's real question — "I started this bot; is it actually running and connected?" — spans three altitudes (operator intent, the process registry's claim, OS/Gateway reality) that drift apart silently (verified 2026-07-03: the registry called three live children `offline` while they held IBKR sockets).

**Related:** ADR 0007 (host-daemon shared-secret auth — **this ADR fulfils its open follow-up, but not as the daemon-side `/diagnose` it sketched**), ADR 0018 (broker session mirror — the socket/identity/recovery authority this ADR *embeds by reference*, never re-implements), ADR 0011 (broker safety verdict / one-connection assumption — **the "one shared IBKR connection serves every instance" line conflicts with the observed per-child socket model this ADR assumes**; recorded as a gap for ADR 0011/0018 to reconcile, not owned here), ADR 0004 (instance-addressed control plane / multi-process registry — the PID↔instance attribution and `fetch_instances` source), ADR 0013 (operator-surface: judgment vs evidence), ADR 0014 (backend-authored trader narratives / Event narrative registry — the closed-vocabulary-plus-authored-copy pattern this ADR reuses), ADR 0015 (operator notice contract). `CONTEXT.md` §§ "Daemon diagnostics — control-plane health", "Broker session mirror — client-connection observability".

## Context

The daemon is a host process (ADR 0003: IBKR error 420 forces it to co-locate with Gateway on the host OS — Windows/Mac/Linux). The containerized data plane reaches it only through an authenticated proxy (ADR 0007: `X-Live-Runner-Token`); **the browser never holds the token and never calls the daemon directly.**

Almost every plumbing fact a diagnostic needs is *already* exposed: code freshness, lease, boot id, and orphan count on `HostRunnerHealth`; transport classification via `DaemonResult` (folded into `RETRYING`/`UNREACHABLE`/`AUTH_FAILED`/… by `DaemonConnectivityMonitor`); the process registry via `fetch_instances`; the socket roster + client-identity reconciliation already authored by the broker session mirror (ADR 0018). What is missing is not facts — it is a **single interpreted, trader-friendly, remediation-carrying report** that composes them and pinpoints a cause.

Three shaping constraints emerged during grilling:

1. **A single "degraded" state is the disease, not the cure.** The operator sees "degraded" across many surfaces and it names nothing actionable. The report's entire value is replacing that one word with specific, individually-remediable, named causes.
2. **A diagnostics surface must never render a control it cannot actuate.** The daemon is a host process; the data plane cannot restart it from inside the container. Pretending otherwise is dishonest.
3. **Redaction is the backend's job.** `/daemon-health` already leaks absolute host paths + full argv to the browser; an *export/share* feature would spread `/Users/<name>/…` into bug reports and screenshots.

## Decision

### 1. One composed authority in the data plane — not a daemon-side `/diagnose`

A single data-plane builder (`app/services/daemon_diagnostics.py`) is the sole authority for daemon/control-plane diagnostic *meaning*. It **authors** the plumbing checks (reachability, auth, contract, code freshness, lease, boot, registry availability, orphans, socket-probe availability — facts only the data plane can observe) and **embeds by reference** the mirror's already-authored socket-reconciliation attention codes. It **never re-runs lsof, never re-classifies a client, never re-authors readiness or runtime-freshness thresholds** (it calls `evaluate_runtime_freshness`), and **never uses the data-plane `IbkrConnectionHealth` as per-bot truth** (that is the singleton/system client).

We rejected a daemon-side `/diagnose` (the shape ADR 0007 sketched) because the daemon structurally cannot author reachability/auth *of itself* — a daemon-side report would still need a data-plane wrapper for the UNREACHABLE/AUTH_FAILED cases, reintroducing two interpretation sites. Composing in the data plane keeps one interpretation authority and requires **no new daemon endpoint** — only additive facts on `HostRunnerHealth` (`platform`/`supervisor`, `lease_threshold_ms`, `lease_write_error`, per-orphan detail) and `exit_reason` on the process status (populated by the daemon's existing `_exit_reason_from_code`, the single mapping authority).

### 2. Primary job — pinpoint why a *specific bot* is failing

The report is not a flat global health check; it is a per-`strategy_instance_id` **diagnostic ladder** that surfaces the **first failing rung** as that bot's `dominant_condition`. A global report still exists for control-plane-wide faults that hit every bot at once (unreachable, auth, stale code, stale lease). The ladder checks **registry/observer availability and mirror amnesia/orphan *before* "never started"** — otherwise a bot the in-memory registry forgot (after a daemon restart) mis-reads as never started. This requires `fetch_instances` as a third compose source: the mirror omits idle/exited bots with no socket row, so process rungs are blind without it.

### 3. A new `DaemonDiagnosticCheck` superset, and "degraded" is abolished

The broker `DiagnosticCheck` (`name/label/status/detail/fix`) cannot carry trader/technical separation, scope, or actions, and its `label` holds code-like strings that would violate the trader-language rule if rendered. A new `DaemonDiagnosticCheck` superset carries a stable-but-never-rendered `check_id`, a closed `category`, `pass|warn|fail|skip` (reused), trader `title`/`summary`, expandable `technical_detail` + structured `evidence`, `remediation`, `scope`, and an optional `action`. The report surfaces a **closed `dominant_condition` enum + backend-authored `headline` copy** (title/summary/remediation) — the same closed-vocabulary-plus-authored-copy pattern as the Event narrative registry (ADR 0014). Every distinct cause is its own named check; `pass|warn|fail` survives only as the severity colour. The endpoint **always returns HTTP 200** with a full report (failure lives in the checks, not the status code), and a top-level `transport` field mirrors `DaemonResult.kind` verbatim.

### 4. Container-actuatable gate — a fix is a button only if the data plane can cause it

A `RECOVERY_MUTATION` action attaches to a check **only** if the data plane can actuate it from inside the container (the daemon executes it in-process on an authenticated forward). v1's only such action is `renew_lease` (reusing `POST /api/live-instances/daemon-health/renew-lease` — non-destructive, no broker, no child process). Host-level fixes (start/restart the daemon) require host process control the container lacks, so they are **structurally never buttons** — only honest guidance, authored per the daemon's **reported OS/supervisor** (`systemctl` / `launchctl` / NSSM). No diagnostic action ever touches the broker or restarts a child.

### 5. Backend-authored redaction; the pre-existing leak is fixed here

The backend is the sole redaction authority; nothing unsafe reaches the browser and the frontend never decides what is safe. Host-absolute paths are reduced to repo-relative with home prefix + hostname stripped; tokens/connection-strings/full-argv are never emitted; operator handles (`run_id`, `strategy_instance_id`, short `boot_id`, `commits_behind`) pass through. There is no per-check frontend exposure gate. The pre-existing `HostRunnerHealth` leak (`repo_root`, `live_runs_root`, `process.log_path`, `process.command` shipped raw) is tightened in the same effort.

### 6. Two presentation surfaces, one report

The same authored report is read by its own snapshot endpoint (`GET /api/live-instances/daemon-diagnose`, global + `per_instance` subreports from one consistent snapshot; optional `/{sid}/daemon-diagnose` projection) **and** embedded as a control-plane header in the broker session mirror page. "Available from both places" is achieved by composition — never by mounting one handler at two routes or fusing the snapshot self-test into the mirror's streaming/paginated payload. The always-visible mirror header binds to the folded monitor state (no probe); a fresh probe fires only on explicit open/refresh (which also distinguishes transient `RETRYING` from terminal `UNREACHABLE` and detects `BOOT_CHANGED`).

## Consequences

**Positive:**
- One interpretation authority for daemon/control-plane diagnostic meaning; the mirror stays the single authority for socket/identity/recovery (embedded, not forked).
- The operator gets a specific, named cause per bot instead of an amber "degraded" blob, with honest per-OS remediation.
- No new daemon endpoint; the daemon stays a thin fact provider (additive fields only).
- Fulfils the ADR 0007 follow-up while closing its `/daemon-health` path/argv leak.

**Negative / costs:**
- Additive daemon-schema churn (`HostRunnerHealth`, `HostRunnerProcessStatus`) plus two small mirror additions (`REGISTRY_SNAPSHOT_UNAVAILABLE`; an attribution-unavailable code for `_argv_for_pid → []`, which today mis-reads a bot-owned PID as a ghost).
- The builder makes two–three daemon-adjacent reads per diagnose (`fetch_health` + `fetch_instances` + mirror snapshot); facts are sub-second-consistent, not atomic. Accepted — snapshot semantics, low stakes.
- Tightening `daemon-health` ripples into the connectivity strip + deploy form consumers.

**Non-consequences:**
- No new authority is created for "is my bot connected" — the mirror keeps it.
- Broker health and daemon health stay separate altitudes; the daemon rungs never bind to `IbkrConnectionHealth`.
- Deferred (not this ADR): deploy/start last-error catalog (source: `mutation_attempts`), clientId-collision specialization (source: broker events / IBKR 326), logs/incidents link-outs, deep WAL/readiness artifact checks.

## References

- `docs/architecture/daemon-diagnostics-proposal.md` — full design, Parts A–I (check catalog, models, endpoints, UX, tests, phasing).
- `docs/reviews/daemon-diagnostics/codex-second-opinion-prompt.md` — the code-verified second-opinion prompt.
- `CONTEXT.md` § "Daemon diagnostics — control-plane health" — decision/vocabulary record.
- `app/engine/live/host_daemon.py`, `host_daemon_client.py`, `daemon_transport.py`, `daemon_connectivity_monitor.py` — the daemon + transport surfaces composed.
- `app/broker/ibkr/diagnostics.py`, `app/broker/ibkr/models.py` (`DiagnosticCheck`) — the peer self-test and the model this one deliberately does not reuse.
- ADR 0007 §"Follow-ups" — the open item this ADR closes.
