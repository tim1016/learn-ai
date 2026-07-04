# Live host-daemon diagnostics — architecture proposal

**Status:** Design (grilled 2026-07-04, Codex second-opinion integrated).
**Scope:** A backend-authored, trader-friendly diagnostics system whose north star
is **pinpointing why a specific bot is failing in the live host daemon**, plus a
global control-plane report for faults that hit every bot at once.
**Glossary:** `CONTEXT.md` § "Daemon diagnostics — control-plane health".
**Turns into:** backend + frontend issues (Part I).

This fulfils the follow-up ADR 0007 left open ("a `/diagnose`-style self-test for
the daemon hop"), but **not** as a daemon-side `/diagnose` — as a *composed* report
authored in the data plane (Part F rationale).

---

## A. Current-state map

### Surfaces that exist today

| Fact | Owner | Surface |
|---|---|---|
| Process registry / live binding (pid, state, start/exit) | **Host daemon** (`RunnerProcessManager._managed`, in-memory) | `/instances`, `/runs/{id}/process` → proxied |
| Code freshness (running SHA vs on-disk HEAD, `commits_behind`) | **Host daemon** (SHA frozen at launch) | `HostRunnerHealth` |
| Control-plane lease (boot_id, lease_status, last_written) | **Host daemon** (`DaemonLeaseWriter`, 1 Hz) | `HostRunnerHealth` |
| Orphan candidates at boot | **Host daemon** (`orphan_classifier`) | `HostRunnerHealth.orphan_candidates_count` (count only) |
| Transport classification (CONNECTED/UNREACHABLE/AUTH_FAILED/PROTOCOL_ERROR/INCOMPATIBLE_CONTRACT) | **Data plane** (`DaemonResult`) | folded by `DaemonConnectivityMonitor` (adds `RETRYING`) |
| lsof socket roster / client identity / recovery | **Broker session mirror** (ADR 0018, built) | `/api/broker/session-mirror` |
| Data-plane's own IBKR connection self-test | **Data plane** (`run_diagnostics` → `DiagnosticCheck`) | `/api/broker/diagnose` |
| Per-instance readiness verdict | **Engine** (live) / **backend** (start-readiness) | `operator_surface`, `/status` |
| Child runtime freshness | **Data plane** (`evaluate_runtime_freshness`) | `/status` |

### The gap

There is **no structured, interpreted, remediation-carrying, trader-friendly daemon
report** analogous to `/api/broker/diagnose`. The daemon story today is a flat
`HostRunnerHealth` envelope + a folded connectivity state. An operator who starts a
bot and cannot confirm it is running has no single surface that says *why*.

### Conflicts surfaced (resolutions in this proposal)

1. **ADR 0011 says "one shared IBKR connection serves every instance"**, but lsof
   showed **per-child** sockets (mirror memo, 2026-07-03). *Resolution:* the mirror
   already owns this reconciliation; diagnostics assumes the per-child model for its
   socket rungs and embeds the mirror's codes by reference. ADR 0011 should record
   the gap (mirror work item, not this one).
2. **`CONTEXT.md` claims the daemon reports its platform/supervisor**, but
   `HostRunnerHealth` has no such field yet (Codex-verified). *Resolution:* Slice 1
   adds it; until then platform-aware guidance degrades to a generic string.
3. **`/daemon-health` leaks absolute host paths + argv to the browser**
   (`repo_root`, `live_runs_root`, `process.log_path`, `process.command`) — a
   goal-#4 violation. *Resolution:* Slice 4 tightens the browser-facing projection.
4. **ADR 0007 phrased the follow-up as a daemon-side `/diagnose`.** This proposal
   deliberately composes in the data plane instead (Part F) — the daemon can't
   author reachability/auth of itself, so a daemon-side report would still need a
   data-plane wrapper, reintroducing two interpretation sites.

---

## B. Diagnostic check catalog (v1)

Every row composes from an existing read: `fetch_health`, `fetch_instances`, the
mirror snapshot, or the connectivity monitor. Global rungs run once; per-instance
rungs run per `strategy_instance_id` and the **first failing rung is the answer.**

### Global rungs (control-plane faults; hit every bot)

| check_id | category | source | pass / warn / fail / skip | dominant_condition | action |
|---|---|---|---|---|---|
| `daemon.reachable` | REACHABILITY | monitor + fresh probe | pass=CONNECTED · warn=RETRYING · fail=UNREACHABLE | `UNREACHABLE` / `RETRYING` | host guidance (platform) |
| `daemon.auth` | AUTH | `DaemonResult` | pass=not 401/403 · fail=AUTH_FAILED · skip=unreachable | `AUTH_FAILED` | host guidance |
| `daemon.contract` | CONTRACT | `DaemonResult` | pass=parses+validates · fail=malformed → `MALFORMED_RESPONSE`; schema → `BUILD_MISMATCH` · skip=up-ladder failed | `MALFORMED_RESPONSE` / `BUILD_MISMATCH` | host guidance (restart) |
| `daemon.code_freshness` | CODE_FRESHNESS | `HostRunnerHealth` git fields | pass=fresh · warn=stale(N behind) · skip=git n/a | `STALE_CODE` | host guidance (restart, platform) |
| `daemon.control_plane_lease` | LEASE | `HostRunnerHealth` lease fields + `lease_threshold_ms` vs now | pass=fresh · warn=stale · fail=`lease_write_error` · skip=unreachable | `LEASE_STALE` / `LEASE_UNWRITABLE` | **`renew_lease` (button)** |
| `daemon.boot_identity` | BOOT | monitor boot-id history | pass=stable · warn=flip since last observation | `BOOT_CHANGED` | navigation (registry) |
| `registry.availability` | PROCESS_REGISTRY | `fetch_instances` | pass=registry read · fail=read failed while health up | `REGISTRY_SNAPSHOT_UNAVAILABLE` | host guidance |
| `orphans.candidates` | ORPHANS | `HostRunnerHealth` orphan detail + mirror | pass=0 · warn=>0 | `ORPHANS_PRESENT` | navigation → mirror |
| `broker.socket_probe` | SOCKET_PROBE | mirror ghost-detection status | pass=available · warn/skip=lsof missing/denied | `SOCKET_PROBE_UNAVAILABLE` | host guidance |

### Per-instance ladder (first failing rung = the answer)

Order matters — availability and amnesia are checked **before** "never started," or a
forgotten-but-running bot mis-reads as never started.

1. **Global transport/auth/contract** — if the daemon is down, *all* bots are undiagnosable; the per-instance ladder short-circuits to a global-fault headline.
2. **Global health facts** — lease, boot, code, orphan-classifier status (context for every instance).
3. **`registry.availability` + `broker.socket_probe`** — can we even read the registry / run lsof?
4. `instance.known` — a deployed/known run exists for this sid → else `NOT_STARTED` only if the next rung agrees.
5. `instance.registry_amnesia` (mirror ref: `REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE`) / `instance.orphaned_socket` (`ORPHANED_BOT_SOCKET`) — **checked before "never started."** → `REGISTRY_AMNESIA` / `ORPHANED_SOCKET`.
6. `instance.process_state` (`fetch_instances`) — idle / running / stopping / exited(+`exit_reason`) → `NOT_STARTED` / `PROCESS_EXITED`.
7. `instance.socket` (mirror row ref) — process live but no socket → `NO_SOCKET` (`STARTED_BUT_NO_SOCKET`).
8. `instance.run_dir_visible` (`_visible_live_run_dir`) + `instance.runtime_fresh` (`evaluate_runtime_freshness`) → `RUN_DIR_INVISIBLE` / `RUNTIME_STALE`.
9. Artifact details (WAL/readiness) — *deferred*; `STALE_CODE` as global context unless no better per-bot cause.

Start-refusal causes (`ACCOUNT_FROZEN`, `CRASH_RETIRED_BLOCKED`, sizing/allowlist) are
**mutation-time** and only diagnosable if persisted to `mutation_attempts` — **deferred**
(Slice 8). Note `ACCOUNT_FROZEN` and `CRASH_RETIRED_BLOCKED` are **separate** conditions.

**Scope column:** global rungs = `GLOBAL`; per-instance rungs = `INSTANCE` (carry
`scope_ref = strategy_instance_id`). Account-level rollup is future.

---

## C. Backend contract (Pydantic v2, `app/schemas/daemon_diagnostics.py`)

```python
from __future__ import annotations
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, JsonValue

DaemonDiagnosticStatus = Literal["pass", "warn", "fail", "skip"]      # reuse broker vocab
DaemonDiagnosticScope  = Literal["global", "account", "instance", "run"]

# Monitor-folded transport (reuses DaemonResult.kind names verbatim + RETRYING):
DaemonTransport = Literal[
    "CONNECTED", "RETRYING", "UNREACHABLE",
    "AUTH_FAILED", "PROTOCOL_ERROR", "INCOMPATIBLE_CONTRACT",
]

class DaemonDiagnosticCategory(StrEnum):
    REACHABILITY = "reachability"; AUTH = "auth"; CONTRACT = "contract"
    CODE_FRESHNESS = "code_freshness"; LEASE = "lease"; BOOT = "boot"
    PROCESS_REGISTRY = "process_registry"; ORPHANS = "orphans"
    SOCKET_PROBE = "socket_probe"; RUNTIME_FRESHNESS = "runtime_freshness"
    ARTIFACTS = "artifacts"

class DaemonDominantCondition(StrEnum):
    HEALTHY = "healthy"; INSTANCE_HEALTHY = "instance_healthy"
    # global
    UNREACHABLE = "unreachable"; RETRYING = "retrying"; AUTH_FAILED = "auth_failed"
    MALFORMED_RESPONSE = "malformed_response"; BUILD_MISMATCH = "build_mismatch"
    STALE_CODE = "stale_code"; LEASE_STALE = "lease_stale"; LEASE_UNWRITABLE = "lease_unwritable"
    BOOT_CHANGED = "boot_changed"; REGISTRY_SNAPSHOT_UNAVAILABLE = "registry_snapshot_unavailable"
    ORPHANS_PRESENT = "orphans_present"; SOCKET_PROBE_UNAVAILABLE = "socket_probe_unavailable"
    # per-instance
    NOT_STARTED = "not_started"; PROCESS_EXITED = "process_exited"
    REGISTRY_AMNESIA = "registry_amnesia"; NO_SOCKET = "no_socket"
    ORPHANED_SOCKET = "orphaned_socket"; RUNTIME_STALE = "runtime_stale"
    RUN_DIR_INVISIBLE = "run_dir_invisible"
    ACCOUNT_FROZEN = "account_frozen"; CRASH_RETIRED_BLOCKED = "crash_retired_blocked"

class DiagnosticEvidence(BaseModel):
    """Structured, already-redacted facts for the technical-details expander."""
    model_config = ConfigDict(frozen=True)
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    redacted: bool = False   # informational: a field was reduced (not a frontend gate)

class DaemonDiagnosticAction(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id: str                                  # closed key, never rendered
    kind: Literal["recovery_mutation", "navigation"]
    label: str                                      # trader button copy
    endpoint: str | None = None                     # data-plane route (recovery only); NEVER a daemon URL
    confirm: bool = False
    deep_link: str | None = None                    # navigation target (mirror / cockpit / runbook)

class DaemonDiagnosticCheck(BaseModel):
    model_config = ConfigDict(frozen=True)
    check_id: str                                   # stable key; NEVER rendered
    category: DaemonDiagnosticCategory
    status: DaemonDiagnosticStatus
    title: str                                      # trader-friendly
    summary: str                                    # trader one-liner
    technical_detail: str | None = None             # evidence prose, expandable
    remediation: str | None = None                  # trader-friendly fix (or platform-aware host guidance)
    scope: DaemonDiagnosticScope
    scope_ref: str | None = None                    # strategy_instance_id when scope="instance"
    evidence: DiagnosticEvidence | None = None
    action: DaemonDiagnosticAction | None = None     # present ONLY when actuatable/navigable

class DaemonDiagnosticHeadline(BaseModel):
    model_config = ConfigDict(frozen=True)
    title: str
    summary: str
    remediation: str | None = None

class DaemonInstanceDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True)
    strategy_instance_id: str
    overall_status: Literal["pass", "warn", "fail"]
    dominant_condition: DaemonDominantCondition
    headline: DaemonDiagnosticHeadline
    checks: list[DaemonDiagnosticCheck]

class DaemonDiagnosticReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    overall_status: Literal["pass", "warn", "fail"]  # aggregate = COLOUR only, never a rendered word
    transport: DaemonTransport
    dominant_condition: DaemonDominantCondition
    headline: DaemonDiagnosticHeadline
    checks: list[DaemonDiagnosticCheck]              # global rungs
    per_instance: list[DaemonInstanceDiagnostic] = Field(default_factory=list)
    daemon_boot_id: str | None = None
    fetched_at_ms: int                               # int64 ms UTC
```

**Enum → trader-copy mapping is backend-owned** (a closed table beside the builder,
same pattern as the event-narrative registry). The frontend renders `title` /
`summary` / `remediation`, colours by `status`, keys/groups by `check_id` /
`category` / `dominant_condition`, and **never maps a raw enum to meaning.**

---

## D. Endpoint design

```
GET /api/live-instances/daemon-diagnose            → DaemonDiagnosticReport   (global + per_instance)
GET /api/live-instances/{sid}/daemon-diagnose      → DaemonDiagnosticReport   (same builder, projected to one sid)
```

- **Always HTTP 200** with a full report, even when the daemon is down. Failure
  lives in `checks` + `transport`, never in the HTTP status. (Contrast
  `/daemon-health`, which stays 502/503 — it is a health probe, not a diagnostic.)
- **Data-plane proxy behavior:** the builder does one consistent snapshot — `fetch_health`
  + `fetch_instances` + one mirror snapshot (one lsof pass) + the monitor's folded
  state — then authors + redacts. The per-sid route projects one instance from the
  same builder (no extra lsof).
- **Auth:** unchanged — the data plane forwards the daemon token from the artifacts
  bind mount (ADR 0007); the browser never holds it and never calls the daemon.
- **Timeouts:** reuse the existing bounds — health `2s`, socket probe `10s`. A wedged
  daemon surfaces as `UNREACHABLE`, not a hung request.
- **Failure rendering:** `transport` mirrors the monitor's folded kind
  (`CONNECTED/RETRYING/UNREACHABLE/AUTH_FAILED/PROTOCOL_ERROR/INCOMPATIBLE_CONTRACT`);
  the reachability/auth/contract rungs carry the fail with distinct remediation;
  everything downstream `skip`s honestly.
- **Recovery action:** `renew_lease` reuses the existing
  `POST /api/live-instances/daemon-health/renew-lease` — the action's `endpoint`
  points there, confirm-gated.
- **New shape, not the broker `DiagnosticCheck`** — the broker model can't carry
  trader/technical separation, scope, or actions, and its `label` holds code-like
  strings that would violate the trader-language rule if rendered. Broker
  `/diagnose` is left untouched (may converge later).

**Daemon-side additions (Slice 1, additive to `HostRunnerHealth` / `HostRunnerProcessStatus`):**
`platform` + `supervisor`; `lease_threshold_ms` + `lease_write_error`;
`orphan_candidates` (per-candidate detail, redacted — not just the count);
`exit_reason` on the process status (via `_exit_reason_from_code`, single mapping
authority). No new daemon *endpoint*.

**Mirror-side additions (Slice 2b):** `REGISTRY_SNAPSHOT_UNAVAILABLE` and an
attribution-unavailable code (for `_argv_for_pid → []`, which today mis-reads a
bot-owned PID as a ghost).

---

## E. Frontend UX

- **Where it lives:** a self-contained **drawer/dialog** opened from the connectivity
  strip's **"Live engine"** link (works app-wide — cockpit, deploy — without
  navigating away mid-incident). The **same report** renders inline as a
  **control-plane header** in the broker session mirror page. One endpoint, two entry
  points. Deep-linkable via a query param.
- **First screen:** transport banner → **headline** (`dominant_condition` copy, never
  the word "degraded") → **checks grouped by category** (trader `title`, status
  colour, one-line `summary`) → per-bot section keyed by `strategy_instance_id`
  showing each bot's first-failing-rung. Technical evidence + `redacted` paths behind
  per-row expanders. `renew_lease` button inline only on a `LEASE_STALE` row.
- **States:** loading → skeleton "Checking live engine…" (never a bare spinner);
  `UNREACHABLE` → headline "Live engine isn't answering — start it on this machine,"
  downstream checks `skip`; `RETRYING` → amber "reconnecting," not red; stale
  snapshot → "as of T" + refresh; no true "empty" state (the transport check always
  exists).
- **Trader-friendliness:** renders only backend-authored copy; `check_id` /
  `category` / `dominant_condition` drive keys/grouping and are **never printed**.
- **No raw identifiers leak:** backend redacts before send (Part G); the frontend has
  nothing unsafe to hide.
- **Export:** "Export report" serializes the already-redacted snapshot (+ a "paths
  redacted for sharing" note). No raw-payload export.
- **Mirror header** binds to the folded monitor state passively (no probe); a fresh
  probe fires only on explicit open/refresh.

---

## F. Single source of truth

| Concern | Owner |
|---|---|
| Check definitions (which rungs exist) | the data-plane **builder** (`daemon_diagnostics.py`) |
| Check status classification (pass/warn/fail/skip) | the builder |
| `dominant_condition` selection (first failing rung) | the builder |
| Trader-facing copy (title/summary/remediation) | backend copy table beside the builder |
| Remediation hints (incl. platform-aware host guidance) | backend, keyed off the daemon's reported OS |
| Display ordering / grouping | backend (`category` + rung order); frontend renders as given |
| Raw technical evidence | backend, **redacted** (`DiagnosticEvidence`) |
| Socket roster / client identity / recovery | **broker session mirror** — embedded by reference, never recomputed |
| Registry (process) state | **host daemon** (`fetch_instances`) — read, not re-derived |
| Runtime freshness thresholds | **`evaluate_runtime_freshness`** — called, not duplicated |
| Readiness / action gates | **cockpit `operator_surface`** — linked authority, not diagnostics-owned |
| Frontend | rendering only — no verdict derivation |

**One authority per fact, even inside the superset.** Diagnostics *authors* the
plumbing rungs and *embeds by reference* the mirror's socket reconciliation; it never
re-runs lsof, never re-classifies a client, never re-authors readiness, and never
treats the data-plane `IbkrConnectionHealth` as per-bot truth.

---

## G. Safety and security

- **Daemon token never leaves the data plane.** All reads/actions go through the
  proxy; the browser holds no token and calls no daemon URL (ADR 0007 preserved).
- **No accidental actuation.** A check is read-only by default; an action attaches
  only when explicitly authored. The **container-actuatable gate** means a
  `recovery_mutation` can only be a fix the data plane can cause from inside the
  container (v1: `renew_lease`, non-destructive). Host-level fixes are structurally
  guidance-only — **the surface never renders a control it cannot actuate.** No
  diagnostic action touches the broker or restarts a child.
- **No machine-path / account leakage.** Backend is the sole redaction authority:
  home prefix + hostname stripped, paths repo-relative, no tokens / connection
  strings / full argv. Operator handles pass through. Export is a serialization of
  the already-redacted report. The pre-existing `daemon-health` leak is fixed
  (Slice 4).
- **Stale ≠ current.** `fetched_at_ms` + "as of T"; `RETRYING` vs terminal
  `UNREACHABLE` distinguished so a transient blip isn't read as down; the mirror
  header binds to the folded state and refreshes on demand.
- **No raw log text.** Logs/incidents are deferred and link out; the report never
  tails daemon logs.
- **Broker health ≠ daemon health.** These are separate altitudes: the daemon rungs
  never bind to `IbkrConnectionHealth`; per-bot broker state is the mirror's.

---

## H. Test plan

**Backend unit (pure builder, offline with fixtures):**
- Each rung → `pass/warn/fail/skip` for its inputs.
- `transport` mapping for all folded kinds; `RETRYING` → warn vs `UNREACHABLE` → fail.
- `dominant_condition` = first-failing-rung, global and per-instance.
- **Socketless bot** B1/B2 diagnosable via `fetch_instances` (regression against the "mirror omits idle/exited" blindness).
- `REGISTRY_AMNESIA` / `ORPHANED_SOCKET` embedded from mirror ref, **not** recomputed.
- `STALE_CODE`, `LEASE_STALE` vs `LEASE_UNWRITABLE` (`lease_write_error`), `ORPHANS_PRESENT` per-bot detail.
- `exit_reason` mapping (1 fatal_halt / 2 operator_refusal / 3 exception / 4 hydration_failure).
- Runtime freshness via `evaluate_runtime_freshness` (missing / invalid / stale).
- **Redaction:** no home prefix, no argv, no token in any field; `redacted` marker set when reduced.
- `ACCOUNT_FROZEN` ≠ `CRASH_RETIRED_BLOCKED` (distinct conditions).

**Backend endpoint (`httpx.AsyncClient` + `ASGITransport`, mock daemon with `respx`):**
- daemon reachable / unreachable / auth-failed / protocol / incompatible → **always 200** + correct headline + `transport`.
- per-sid projection == the matching subreport in the global report.
- `renew_lease` action routes through the proxy (not the daemon directly).

**Frontend (Vitest + Testing Library):**
- Renders backend-authored `title`/`summary`/`remediation` **without deriving meaning** (assert authored copy shows, not a client-computed verdict — mirrors the Playwright meta-rule).
- unreachable / stale / loading states; `RETRYING` shows amber "reconnecting," not red.
- A host-only fix shows guidance and **no button**; `renew_lease` button only on `LEASE_STALE`.
- No raw path/argv/token in the DOM; export contains only redacted content.
- AXE clean; WCAG AA (focus, contrast, ARIA on the drawer).

---

## I. Phased implementation plan (PR-sized slices)

| # | Slice | Stack | Depends |
|---|---|---|---|
| 1 | **Daemon additive facts** — `HostRunnerHealth`: `platform`/`supervisor`, `lease_threshold_ms`, `lease_write_error`, orphan-candidate detail; `HostRunnerProcessStatus.exit_reason`. Backward-compatible. | backend (daemon) | — |
| 2 | **Schema + pure builder** — models + closed enums; builder composes health + instances + mirror + monitor → report, with redaction + first-failing-rung ladder. Unit-tested offline. | backend (data-plane) | 1 |
| 2b | **Mirror-side deps** — `REGISTRY_SNAPSHOT_UNAVAILABLE` + attribution-unavailable code. Rides with 2. | backend (mirror) | — |
| 3 | **Transport + endpoints + tests** — `/daemon-diagnose` (global + `per_instance`) and `/{sid}/daemon-diagnose`; always-200; `renew_lease` wired. Endpoint tests. | backend | 2 |
| 4 | **Redaction hardening** — fix the `daemon-health` path/argv leak; update connectivity strip + deploy form. | backend + frontend | 1 |
| 5 | **Frontend service + types** — typed client + drawer state model. | frontend | 3 |
| 6 | **Drawer UI + mirror header embed** — drawer from "Live engine"; control-plane header in mirror; states; per-check expanders; `renew_lease` button; AXE/WCAG. | frontend | 5 |
| 7 | **Copy/report polish + export** — trader-copy pass; export the redacted snapshot. | frontend | 6 |
| 8 | *(deferred)* deploy/start last-error catalog (`mutation_attempts`); clientId-collision via broker events; logs/incidents link-outs; deep WAL/readiness checks. | later | — |

**v1 = slices 1–7.** Slice 8 is explicitly out of the first shippable increment.
