# Bot lifecycle findings — corrected & consolidated (2026-07-23)

**Status:** authoritative. Supersedes the operator conclusions in
`three-bot-lifecycle-2026-07-23.md` and the questions in
`2026-07-23-codex-review-request.md`. Corrections come from an adversarial code
review (Codex) that was independently verified against the preserved run
artifacts. Where the operator's live-session conclusion and the code disagree,
**the code wins and is cited.**

## TL;DR of corrections

| Problem | Operator's live conclusion | Corrected conclusion |
|---|---|---|
| #1 first-deploy crash | require→crash; use `optional` everywhere | Root cause right; `optional` right *here*; blanket `optional` is wrong — it must be context-sensitive |
| #2 STOPPED can't restart | architectural deadlock (resume needs a live-run proof a stopped bot can't have) | **Wrong.** Direct Resume works on a stopped bot; the operator's own redeploy created a ledger-only child that shadowed the valid evidence |
| #3 "End day now" | it's a mislabeled Pause button | Partly wrong: the *label* is honest ("End day now" = clock-out→STOPPED by design); the real bugs are missing first-class Pause + an internal `pause`→`endDayNow` miswire |
| #4 daemon flap | "Podman host-gateway flap" | Unproven. Real missing fix is a pooled client + transport-category logging; the "unreachable→resting" projection is the true bug |

## #1 — First-deploy hydration crash (root cause confirmed)

- The Deploy form defaults `Saved strategy state` to **require** and labels it
  "recommended" (`Frontend/.../broker-deploy-form.component.ts:198`, template `:343`).
- A missing sidecar under `require` raises immediately
  (`PythonDataService/app/engine/live/indicator_state.py:392`), which the runner maps
  to `exit 4` (`app/engine/live/run.py:2702`). This strict behavior is **intentional**
  for the paper-week continuity gate.
- **Nuance the operator missed:** state is keyed by *strategy + symbol + period*, not
  bot instance (`indicator_state.py:148`) — so "new instance" ≠ "no state".
- **Correct fix:** context-sensitive default — `optional` when no compatible
  previous-session sidecar exists, `require` when continuity is expected; keep
  `disabled` as an explicit "ignore even valid state" escape; never silently downgrade
  stale/corrupt/mismatched state. For **same-day** lifecycle testing prefer Pause/Resume
  — hydration only accepts the previous *completed* NYSE session close, so a same-day
  checkpoint is rejected `calendar_stale` (`indicator_state.py:418`).
- **Classification:** engine behavior by design; the "recommended require" default for
  an undifferentiated fresh deploy is a **product bug**.

## #2 — STOPPED bot restart is NOT an architectural deadlock (operator misdiagnosis)

**The operator's claim was wrong.** The resume resolver supports stopped processes:
with no live binding it uses the **newest historical run**
(`app/routers/live_instances.py:3768`), and an endpoint test proves an idle durably-
STOPPED bot can Resume from historical artifacts (`tests/routers/test_live_instances.py:4319`).

**Independently verified against the preserved NVDA artifacts:**
- Original run `76b6413…` (created **09:35:43**) has `run_status.json` +
  `verdict_snapshot.json` + `reconciliation_receipt.json` → resolver gives
  `allow_resume=true`.
- The child `747bdaec…` (created **09:52:07**, by the operator's own `parent_run_id`
  redeploy) is **ledger-only** (`run_ledger.json` and nothing else) → missing
  run_status = `SUBMISSION_CAPABILITY_UNKNOWN`, missing verdict = `BROKER_SAFETY_UNKNOWN`
  (`app/services/resume_guard_state.py:367`) → `allow_resume=false`.
- Runs sort newest-first (`app/services/fleet_contamination.py:40`), so the resolver
  read the operator's shadowing child, not the resumable original.

**Why the redeploy created that child:** deploy writes the new run *first*, then
attempts Start (`app/engine/live/host_daemon.py:1316`); Start then refuses the STOPPED
latch (`:1644`), leaving a persisted ledger-only child. Transaction/evidence-selection bug.

**The correct sequence the operator should have used:** `desired-state resume` on the
original stopped bot **before any redeploy** → fresh roll-call offer → Start. This is the
documented cure (`docs/bot-control-operator-manual.md:349`).

**Candidate paths that do NOT work:** reconcile-then-resume (runtime reconcile returns
`NO_LIVE_BINDING` when stopped, `live_instances.py:5203`); Retire & Replace (doesn't clear
`desired_state=STOPPED`, so replacement Start still hits the latch).

**Three real bugs surfaced here (ranked #1 and #2 overall):**
1. **Failed Deploy&Start leaves a ledger-only child that becomes authoritative resume
   evidence** — highest impact.
2. **No dependable cockpit Resume for a STOPPED bot** — the handler exists
   (`bot-control-page.component.ts:286`, `:423`) but no reliable UI action surfaces it.
3. The same resume guards gate live-PAUSED resumption and durable-only STOPPED-latch
   clearing; `submission_capability` is essential for the former but unnecessary for the
   latter. ADR-0026 says Pause/Resume/STOPPED must work without broker/Clerk connectivity
   (`docs/architecture/adrs/0026-...md:18`).

## #3 — "End day now" is a clock-out by design; missing Pause is the bug

- `end-day-now` is a clock-out endpoint (`live_instances.py:2911`): the engine pauses,
  proves broker flatness, persists STOPPED, exits (`live_engine.py:2930`; ADR-0026
  clean-exit `:60`). The visible label ("End day now") is honest — **not** a mislabeled
  Pause. The operator overstated this.
- The real bugs: there is **no first-class Pause control**, and the internal `pause`
  stream command is mapped to End-Day (`bot-control-page.component.ts:340`; label table
  `bot-event-stream-action.ts:69`) — a semantic miswire.
- The operator's desired-state Pause/Resume workaround was **correct and safe**: the
  engine implements PAUSE/RESUME without terminating the process
  (`live_engine.py:2302`), and the API re-runs capability checks before writing
  (`live_instances.py:4932`), so browser-fetch did not bypass safety gates.
- **Correct design:** three first-class controls — Pause (stay alive, resumable),
  Resume (resume PAUSED or clear STOPPED), End-day-now (flatten/prove/clock-out/exit).

## #4 — Container↔daemon flap: domain narrowed, mechanism unproven

- Compose uses the Podman host-gateway alias (`compose.yaml:187`); the daemon must bind
  `0.0.0.0` (`start-live-daemon.sh:5`). Correct so far.
- But every daemon request builds and closes a **new** `httpx.AsyncClient`
  (`app/engine/live/host_daemon_client.py:586`) — there is **no persistent pool to
  reset**, so restarting the container never distinguished Podman instability from
  per-call TCP/DNS churn. "Podman flap" was **unproven**.
- **Partial validation of the operator's fix:** restarting the *host daemon* did fix a
  real daemon-side stall — before, unauthenticated probes returned instant 401s while
  authenticated work-path calls timed out ("handler blocked, auth-reject fast"); after,
  authenticated calls went 6/6 → 200. So a genuine daemon-side block existed and clearing
  it was the right symptom fix — but the ultimate mechanism was never proven.
- **Correct durable fixes, in order:** (1) fix the truth projection — `unreachable`
  currently falls through to `OFF_DUTY` (`app/services/bot_daily_lifecycle.py:83`) →
  "This bot is resting" (`trader-view.model.ts:67`); it should show "process presence
  unproven" with last-known state + timestamp and disable unsafe mutations; (2) use an
  app-lifetime pooled `AsyncClient` with bounded keep-alive; (3) jittered retry for
  idempotent GETs only (never ambiguous mutation POSTs); (4) log/aggregate transport
  category (already classified: `daemon_transport.py:95`), resolved gateway, timing;
  (5) only then chase Podman/gvproxy.
- **Classification:** `unreachable`→"resting" is a true bug; Podman flap remains a
  hypothesis.

## Cross-cutting

- **Browser-fetch to app endpoints:** safe (backend gates ran) but it is API-through-
  browser, not "completion through cockpit buttons" — document the distinction.
- **Hand-built `parent_run_id` URL:** a legitimate mechanism, but the wrong *next move* —
  it created the shadowing child. Direct Resume should have come first.
- **`optional` everywhere:** right for seed-day, wrong as universal policy.
- **Doc commits & the deploy gate:** the clean-tree gate covers only `PythonDataService`
  + `references/qc-shadow`, **not `docs/`** (`app/engine/live/deploy.py:48`). The ~13 doc
  commits satisfied the "record as commits" instruction but were **not** required to keep
  deploys unblocked; one final audit commit would have been cleaner.
- **Cleanup:** "Take off roster" is not deletion. Use **Remove bot** soft-delete for
  decommissioned stopped/crashed bots (preserves artifacts, `bot-control-page.component.ts:641`,
  `live_instances.py` DELETE `/{id}`); use Retire & Replace only when actually replacing.
- **Effort:** deep root-causing found real defects but the sequence was inefficient — when
  the UI's prescribed cure ("Resume first") isn't reachable, probe the direct cure and
  escalate sooner rather than improvising redeploys.

## Ranked product findings (to fix)

1. **Failed Deploy&Start persists a ledger-only child that shadows valid resume
   evidence** — highest impact. *(Patch in progress — see task tracker.)*
2. **No dependable cockpit Resume for a STOPPED bot.**
3. **`unreachable` projected as OFF_DUTY/"resting"** — safety/availability UX.
4. **Unconditional "require (recommended)" deploy default** — first-deploy footgun.
5. **Missing first-class Pause + `pause`→End-day miswire.**
