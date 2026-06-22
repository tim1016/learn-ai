# PRD #619-B program — Codex completion handoff

**Date:** 2026-06-21
**Author:** Claude (Opus 4.7, 1M context)
**Audience:** Independent reviewer / next implementation agent.
**Scope:** Status of PRD #619-B (typed daemon connectivity + runtime ownership recovery), including the completed operator-surface and session-aware freshness work.

You can pick this up cold. No prior session context required.

---

## Completion update — Codex implementation

The two items that were still mapped but not started are now implemented on
`prd-619-b-engine-watchdog-wiring-v2`:

1. **Operator-surface + cockpit integration**
   - The data plane reads the bound child's `engine_runtime.json` directly.
   - Missing artifacts surface `ENGINE_RUNTIME_MISSING`; malformed or
     forward-incompatible artifacts surface
     `ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE`.
   - `OperatorSurface.runtime_freshness` carries per-domain states, ages,
     reason codes, the aggregate reason-code list, and `posture_demoted`.
   - Resume and Flatten-and-pause disable with `POSTURE_DEMOTED`.
   - Pause and Stop intentionally remain available as fail-safe durable
     intents; Mark-poisoned remains available for incident recovery.
   - Mutation endpoints consume the same freshness gate as status.
   - The cockpit renders a high-visibility `LAST-KNOWN` / `ATTENTION`
     runtime banner from backend-authored reason codes.

2. **Session-aware bar-loop freshness**
   - Session state is evaluated by the backend at read time through
     `pandas_market_calendars`, including weekends, holidays, and early
     closes.
   - This deliberately does **not** add session state to `BarLoopBlock` and
     does not bump `engine_runtime.json` schema version. Session phase is a
     time-of-evaluation context, not a child-owned runtime fact.
   - The calendar can prove `RTH_OPEN` / `CLOSED`; it does not synthesize
     `HALTED` without a separate market-status observation.

Verification receipts:

- Focused Python router/service/calendar suite: **205 passed** after the
  final fail-safe action-policy adjustment.
- Frontend focused contract/component: **10 passed**.
- Frontend production build: **passed** after clean `npm ci`.
- Dependency contract:
  `echarts@5.4.3`, `echarts-gl@2.0.9`, `zrender@5.4.4`.
- Project Python first pass: **5,146 passed / 45 skipped / 12 failures**.
  Three 619-B failures identified the Pause/Stop safety-policy issue and were
  corrected; the remaining nine are unrelated cache, reference, launcher,
  environment, and pre-existing engine defects.
- Project Frontend run: **1,112 passed / 78 inherited failures**, all
  concentrated in unrelated suites whose test environment lacks
  `localStorage`.

The original recommendation to disable Pause and Stop during posture
demotion was rejected during implementation because it removes the safest
operator controls exactly when runtime authority is degraded. ADR-0004 now
records the asymmetric action policy.

---

## 1. What PRD #619 is

A multi-PR program decomposed during a grilling session into four PRs:

| PR | Theme | Status |
|---|---|---|
| **619-A** | Cockpit correctness + verdict_provider wiring + ADR-0011 amendment | **Merged** (#620) |
| **619-B** | Typed daemon connectivity, runtime ownership, control-plane recovery | **In progress** (see §2) |
| **619-C** | Fail-closed ownership recovery | Not started |
| **619-D** | (TBD post 619-C) | Not started |

619-B itself was decomposed into B1–B8:

| Slice | What | Where |
|---|---|---|
| B1 | `engine_runtime.json` schema + atomic writer | Merged in #621 (foundation) |
| B2 | In-memory aggregator + serialized publisher task | Merged in #621 |
| B3 | Wire publisher into LiveEngine | Merged (#625) |
| B4 | Daemon `boot_id` + lease + DRAINING state | Merged in #621 |
| B5 | Child watchdog (lease-loss → PAUSED → bounded exit) | Merged (#626) |
| B6 | Orphan classification on daemon boot | Merged in #621 |
| B7 | Backend freshness evaluator | Merged (#627) |
| B8 | ADR-0004 amendment | Merged (#627) |
| **Daemon FastAPI integration** | Lifespan wires lease writer + boot_id env + orphan classifier | **Merged** (#628) |
| **Engine ChildWatchdog wiring** | LiveEngine accepts `watchdog_factory`, constructs watchdog from daemon env | **PR #629 open; CI green** |
| **Operator-surface composer + cockpit integration** | Fold `posture_demoted` into action capability, surface `stale_reason_codes` | **Implemented locally** |
| **Bar_loop session-state provider** | Feed calendar-authored `RTH_OPEN`/`CLOSED` into freshness evaluator | **Implemented locally** |

---

## 2. PR #629 — engine ChildWatchdog wiring (the open one)

**Branch:** `prd-619-b-engine-watchdog-wiring-v2`
**Commit:** `4a6b832a`
**Diff:** ~129 LoC engine wiring + 3 wiring tests + 1 helper function in `run.py`.

### What it does

Closes the seam between the host daemon (#621, #628) and the engine.

1. `LiveEngine.__init__` gains a `watchdog_factory: object = None` arg.
2. In `LiveEngine.run()` startup, if `watchdog_factory is not None` AND the runtime aggregator is present AND a shutdown event exists, the engine calls `_start_child_watchdog(shutdown_event)`, which:
   - Builds four engine-side callbacks: `block_submissions` (flips a new `_submissions_blocked` flag that `submit_order_async` honours alongside `_paused`), `persist_paused(reason)` (writes `DesiredState.PAUSED` with reason `control_plane_lease_lost:<…>`), `disconnect_broker()` (awaits `_client.disconnect()`), `request_engine_exit()` (sets the shutdown event).
   - Calls `self._watchdog_factory(...)` with those callbacks + the aggregator.
   - Awaits `watchdog.start()`.
3. Engine shutdown awaits `watchdog.stop()` before cancelling the command-poll task.
4. `run.py` exports `_build_child_watchdog_factory(artifacts_root, run_dir)` — a closure that reads `LIVE_RUNNER_DAEMON_BOOT_ID` from env (set by `host_daemon._build_child_env` in #628) and returns a factory that constructs a `ChildWatchdog` with the right boot_id. When the env var is absent (CLI-without-daemon path), it builds a watchdog with `expected_daemon_boot_id=None` — still detects expired leases, skips the `BOOT_ID_CHANGED` check.

### Files touched

- `PythonDataService/app/engine/live/live_engine.py` (+92 LoC)
- `PythonDataService/app/engine/live/run.py` (+39 LoC)
- `PythonDataService/tests/engine/live/test_live_engine_watchdog_wiring.py` (new, 3 tests)

### Test surface

- 3 new wiring tests pin the factory contract: signature shape, boot_id env propagation (both with and without `LIVE_RUNNER_DAEMON_BOOT_ID`).
- 5-step contract itself is exercised end-to-end by `tests/control_plane/test_child_watchdog.py` (already on master from #626).
- Project-scope: `pytest tests/engine/live tests/control_plane` → **945 passed / 54 skipped / 0 failures** (1 pre-existing test deselected — path-resolution failure on master).

### Caveat surfaced in the PR description

Thermo-nuclear-code-quality-review was **skipped at user direction** for this PR (CLAUDE.md normally gates first push on it).

---

## 3. Design plan for the two remaining follow-ups

These are mapped from independent research-agent passes over the codebase. File:line citations are real; everything is read-only research.

### 3a. Operator-surface composer + cockpit integration

**Goal:** When `RuntimeFreshness.posture_demoted=True`, the cockpit's start/resume/pause/stop buttons should disable, and the operator should see `stale_reason_codes` rendered visibly.

**Backend seams (4 Python files):**

1. `PythonDataService/app/routers/live_instances.py:1284–1300` — router currently calls `compute_operator_surface()` without freshness. Add: load `engine_runtime.json` from live_binding run dir → call `evaluate_runtime_freshness(snapshot, session_state=None)` → pass result to composer.
2. `PythonDataService/app/services/operator_surface.py:466–518` — add `runtime_freshness: RuntimeFreshness | None = None` to `compute_operator_surface()`, forward to `evaluate_all_actions()`.
3. `PythonDataService/app/services/operator_capability.py:140–224` — `evaluate_action()` gets `runtime_freshness` kwarg. In resume/pause/stop blocks, append `POSTURE_DEMOTED` reason code when `runtime_freshness and runtime_freshness.posture_demoted`.
4. (Optional) Expose `runtime_freshness` as a nested optional field on the `OperatorSurface` GraphQL type so the cockpit can render `stale_reason_codes` directly rather than deriving them from `disabled_reasons[]`.

**Frontend seams (2 files):**

1. `Frontend/src/app/api/live-instances.types.ts:322–338` — add optional `runtime_freshness` to `OperatorSurface` interface.
2. `Frontend/src/app/components/broker/cockpit-v2/cockpit-shell.component.html:83–85` (existing error-banner pattern) **or** `status-risk-tab.component.ts` (existing readiness_gates rendering with suggested_action) — render `stale_reason_codes` as a banner/chip.

### 3b. Bar_loop session-state provider

**Goal:** Currently the freshness evaluator receives `session_state=None` and falls back to threshold-only checks on the bar_loop. With session state, it can distinguish "no bars for 60s because the market is closed" (`NOT_APPLICABLE`) from "no bars for 60s during RTH" (`DEGRADED`).

**Two paths considered:**

| | Path A (extend BarLoopBlock) | Path B (separate sidecar) |
|---|---|---|
| Mechanism | Add `current_session_state: SessionState \| None` field, bump `EngineRuntimeSnapshot.schema_version` 1→2 | New `session_state.json` atomic-written sidecar |
| Pros | Single atomic artifact, no new file I/O, smaller diff | Decouples session from bar timing, reusable by other domains |
| Cons | Schema version bump, BarLoopBlock takes a session-shaped field that isn't strictly "bar loop" | Extra atomic-write pipeline, two artifacts to keep coherent |
| Recommendation | **Start here** | Upgrade later if broker / control_plane also need session awareness |

**Producer side:**
- `PythonDataService/app/engine/live/live_engine.py:2082–2104` — `_publish_bar_loop_block(minute_bar)` is the existing call site. Add `nyse_calendar.session_state_at_ms(now_ms)` lookup (file: `PythonDataService/app/engine/live/nyse_calendar.py:29–59`, authoritative NYSE hours via `pandas_market_calendars`).

**Schema side:**
- `PythonDataService/app/engine/live/engine_runtime.py:107–124` — `BarLoopBlock` is frozen + forbid-extra; extending it requires a schema version bump.

**Consumer side:**
- `PythonDataService/app/services/runtime_freshness.py:60` defines `SessionState = Literal["RTH_OPEN", "CLOSED", "HALTED"]`.
- `runtime_freshness.py:145–165` — `_evaluate_bar_loop()` already does the right thing when `session_state` is non-None: CLOSED → `NOT_APPLICABLE` + `BAR_LOOP_SESSION_CLOSED`, HALTED → `DEGRADED` + `BAR_LOOP_SESSION_HALTED`.

---

## 4. Sequencing argument

The composer wiring and session-state provider are **independent**, but the session-state provider **enriches** what the composer surfaces. The freshness evaluator already accepts `session_state=None` (threshold-only fallback), so the composer can ship first and benefit from session enrichment once it lands — no rework in the composer PR.

Proposed order:
1. **PR #629** (open) — engine ChildWatchdog wiring.
2. **PR-next-1** — operator-surface composer + cockpit. Uses `session_state=None` as a starting point.
3. **PR-next-2** — bar_loop session-state provider (Path A). Composer PR-next-1 starts consuming richer data with zero further code change.

---

## 5. Open questions for the second opinion

### Q1. Engine watchdog wiring placement
`_start_child_watchdog` lives inside `LiveEngine` (~30 LoC). `LiveEngine` is already a large file — should this be extracted to a sibling module (e.g. `app/engine/live/watchdog_wiring.py`)? My read: the four callbacks need closure over engine internals (`_submissions_blocked`, `_persist_desired_state`, `_client`, the shutdown event), so an extraction would just trade one closure for an explicit dependency-injection object. Keeping it inline preserves locality. Want a second read on whether that's the right call.

### Q2. Sequencing — should PR-next-1 wait for PR-next-2?
Shipping composer with `session_state=None` means the bar_loop posture-demoted check uses threshold-only fallback for one PR cycle. That's not wrong — the threshold *is* a posture indicator — but it does mean the cockpit will show `BAR_LOOP_STALE` rather than `BAR_LOOP_SESSION_CLOSED` during off-hours during the gap. Is that acceptable, or should we bundle?

### Q3. Path A vs Path B for session state
Path A bumps `EngineRuntimeSnapshot.schema_version` 1→2 and adds a field that's *about* the session, not the bar loop, onto a block named `BarLoopBlock`. The naming is slightly off-key. Path B is cleaner *in name* but doubles the artifact surface. Worth the indirection?

### Q4. GraphQL shape
Should `runtime_freshness` be a nested optional field inside `OperatorSurface`, or a separate top-level field on the live-instance query? The composer already owns "everything the operator sees on this instance"; nesting is consistent. But `RuntimeFreshness` is also useful outside operator context (incidents page, debugging). Wondering if it deserves its own resolver.

### Q5. Cockpit UX
Three render options for `stale_reason_codes`:
- (a) A persistent banner above the action row (high visibility).
- (b) A chip next to each disabled action (close to the disable reason).
- (c) A gate card in the status-risk tab (matches the existing `readiness_gates` UX).
The research agent leaned toward (a)/(c). My read: (c) is most consistent with existing patterns, but (a) is more honest about urgency when posture is demoted. Curious which you'd pick.

### Q6. The pre-existing lint nit
`tests/scripts/test_regenerate_cross_engine_study.py` has an I001 import-sort violation that reproduces on master HEAD (verified with `git stash` + lint). Local container ruff is 0.15.18 (CI parity); the violation may be hidden in CI by an older ruff. Worth a drive-by fix in either PR-next-1 or as its own one-line commit? I left it alone on #629 per "don't silently reformat unrelated code as part of your task."

---

## 6. Files to read first for grounding

If you want to verify rather than trust:

- `PythonDataService/app/engine/live/live_engine.py:418–430` — `watchdog_factory` constructor surface + docstring.
- `PythonDataService/app/engine/live/live_engine.py:2123–2164` — `_start_child_watchdog` body.
- `PythonDataService/app/engine/live/run.py` — search for `_build_child_watchdog_factory` (helper) and `watchdog_factory=` (call site).
- `PythonDataService/app/engine/live/child_watchdog.py:120–141` — `ChildWatchdog.__init__` constructor signature (the contract the factory has to honour).
- `PythonDataService/app/services/runtime_freshness.py:60, 89, 145–165, 226, 248–252` — freshness evaluator's public shape.
- `PythonDataService/app/services/operator_capability.py:140–224` — current action-disable evaluator.
- `Frontend/src/app/components/broker/cockpit-v2/cockpit-shell.component.html:83–85` — existing error-banner pattern.

---

## 7. What I'd like back

A short note (no need to write code) hitting:

1. Q1–Q5 verdicts, with one-line reasons.
2. Any red flags in the design that aren't on the open-questions list.
3. Anything you'd push back on in the sequencing argument (§4).

Thanks.
