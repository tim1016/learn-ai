# Review request for Codex — bot lifecycle operations, 2026-07-23

**Purpose of this document.** An operator (Claude) drove a live paper-trading bot
lifecycle test through the UI, hit four non-trivial problems, and applied a
solution or workaround to each. We want a second opinion: **for each problem, was
the chosen solution correct, and is there a better one?** Please be adversarial —
verify the root-cause claims against the code, and challenge the workarounds.

Full blow-by-blow with timestamps: `docs/audits/three-bot-lifecycle-2026-07-23.md`.
Everything below is committed to `master`.

---

## The task

Run a supervised bot-lifecycle exercise on paper account `DUM284968` entirely
through the cockpit UI: launch 1 bot → stagger to 3 (SPY/QQQ/NVDA, EMA-crossover,
paper submit, 2000 orders/day) → hold 3 concurrent 15 min → stop+restart one →
stop+restart two → stop all three → wait 10 min → restart one older bot + launch
two new (AAPL, MSFT). Record progress as commits on `master` (the deploy page
refuses to operate with a dirty tree).

**Outcome:** all steps completed; account stayed CLEAN/flat throughout. But the
"stop/restart" half only worked after discovering the intended in-place restart is
effectively unavailable, and required driving lower-level endpoints from the
browser. The four problems and solutions below are what we want reviewed.

---

## Problem 1 — Every brand-new deploy crashes on start ("Require saved state")

**Symptom.** A freshly deployed bot connected to IBKR paper fine, then crashed:
`ERROR __main__ indicator-state hydrate failed (missing)`, `exit_code 4`,
`exit_reason "exception"`. `indicator_state_hydration.json` showed
`policy=require`, expected `live_state/ema_crossover_signal/SPY_15m.json`,
`accepted=false`, `failure_reason="missing"`.

**Root cause (claimed).** The Deploy page's Advanced start setting defaults to
**"Require saved state (recommended)"**. A brand-new instance has no saved
indicator state, so `require` turns the expected absence into a fatal crash.

**Solution applied.** Re-deploy with **"Use saved state when available"**
(`optional`) → started clean. Used `optional` for every subsequent deploy.

**Verify / critique against:** the hydrate-policy handling in
`PythonDataService/app/engine/live/` (indicator-state hydration) and the deploy
form's `Advanced start settings` (`Frontend/.../broker-deploy-form`).

**Questions.** Is `require` a sane *recommended default* when it hard-crashes every
first deploy? Should the default be `optional`, or should first-run be auto-detected
(no prior run ⇒ don't require state)? Is `optional` vs `disabled` ("Start fresh")
the right choice for a first deploy, and does `optional` correctly pick up saved
state on a genuine restart?

---

## Problem 2 — A gracefully-STOPPED bot cannot be restarted in place (deadlock)

**Symptom.** Graceful stop ("Stop bot gracefully") sets `desired_state=STOPPED` and
the run **process exits**. The cockpit then offers only **"Start"**, which is
refused: *"This bot is durably STOPPED. Resume it before starting."* No **Resume**
control is surfaced anywhere in the cockpit (Trader view, Operations, lifecycle
cards, "…" menu) for that state.

**What we tried.**
1. Cockpit **Start** → refused (durably STOPPED).
2. **Same-name redeploy** (Deploy & run) → HTTP 409 "Deployment name is already
   used by an existing strategy instance."
3. **Redeploy-from-run** — the app's real same-instance redeploy path, opening the
   deploy page with `?parent_run_id=<run>` (see
   `Frontend/.../lib/deploy-prefill-params.ts:redeployQueryParamsForStatus`, which
   sets `parent_run_id`; and `deploy.py:_existing_run_for_strategy_instance` /
   `allow_same_instance_redeploy`). Got past the name check but → HTTP 409
   **"Stopped Requires Resume"** ("Use Resume to set desired_state=RUNNING, then
   start").
4. **Called the real resume operation** the UI button would call — `POST
   /api/live-instances/{id}/desired-state {action:'resume'}` (from the browser,
   through the dev proxy so the control secret was attached). Response: **HTTP 409,
   `allow_resume:false`**, reasons **`BROKER_SAFETY_UNKNOWN` +
   `SUBMISSION_CAPABILITY_UNKNOWN`** with detail *"run_status.json absent"*.

**Root cause (claimed).** The resume gate (`live_instances.py:set_instance_desired_state`
→ `evaluate_action("resume", …)`, guard fields `broker_safety` /
`submission_capability`) requires a **live run's** broker/submission proof. A
fully-STOPPED bot has **no run** ⇒ no `run_status.json` ⇒ proof `UNKNOWN` ⇒ resume
permanently refused. i.e. **resume is designed for a PAUSED bot (run still alive),
not a STOPPED one** — so "Stop → restart in place" has no path. Also: the frontend
*implements* resume (`bot-control-page.component.ts`: `setIntent('resume',…)` →
`setInstanceDesiredState({action:'resume'})`; error text "…Resume the bot to clear
the stop latch") but never *surfaces a button* for it in the STOPPED state.

**Solution applied.** Abandoned in-place restart of the stopped instance; restored
the bot with a **fresh new-name deploy**.

**Verify / critique against:** `PythonDataService/app/routers/live_instances.py`
(`set_instance_desired_state` ~L4911, the resume gate and `SetDesiredStateRequest`),
`app/engine/live/deploy.py` (`allow_same_instance_redeploy`), `app/services/resume_guard_state.py`,
`Frontend/src/app/components/broker/bot-control/bot-control-page.component.ts`.

**Questions (the important ones).**
- Is this a **genuine architectural deadlock**, or did we miss the intended
  restart path? e.g. is there a **reconcile-then-resume** sequence
  (`reconcile_instance`) that would establish `broker_safety`/`submission_capability`
  before resume? Would that also fix `submission_capability` given there's still no
  run?
- Should the resume gate require `submission_capability` (a *run* property) for a
  STOPPED bot at all, or only `broker_safety` (account-level)? Is requiring a live
  run's proof to *start* a run a design bug?
- Is **"Retire & Replace"** the sanctioned restart-a-stopped-bot path (it was the
  only action offered on the stopped row)? If so, does it re-use the require-policy
  and re-crash (Problem 1)?
- Is the missing **Resume button** in the cockpit a regression (prior-session notes
  say restart "worked before"), or intended?

---

## Problem 3 — The UI "pause" button is actually a stop; the real pause is elsewhere

After Problem 2, the operator (per user direction) switched "stop→restart" to
**pause/resume**. Two findings:

**Finding A — "End day now" ≠ pause.** The frontend wires the `pause` action to
`dispatchEndDayNow()` → `endDayNow(id,{force:false})` →
`POST /api/live-instances/{id}/end-day-now`. Triggering it set
`desired_state=STOPPED` and the **run process exited** (verified: pid gone). i.e.
the UI's "pause"/"End day now" is a **clock-out → STOPPED**, which then deadlocks
exactly like Problem 2. (This cost us an instance.)

**Finding B — the real resumable pause is the desired-state action.**
`POST /api/live-instances/{id}/desired-state {action:'pause'}` sets
`desired_state=PAUSED` and the **process stays alive** (`run.py`: "Booting paused…
durable desired_state=PAUSED"). Verified on `qqq-0723`: after `pause`, the pid
stayed ALIVE with `PAUSED`; after `resume`, `RUNNING`, actuated on the same live
run — **no deadlock**, because the live run supplies the proof the resume gate
needs.

**Solution applied.** Drove all pause/resume via
`POST /api/live-instances/{id}/desired-state {action:'pause'|'resume'}` from the
browser (through the proxy), **not** the UI buttons — because the UI "pause" button
is the wrong (clock-out) action.

**Verify / critique against:** `bot-control-page.component.ts` (`case 'pause' →
dispatchEndDayNow`), `live_instances.py` (`end-day-now` endpoint ~L2911 vs
`desired-state` ~L4911), `app/engine/live/run.py` (`start_paused` / "Booting paused"),
`app/engine/live/desired_state.py`.

**Questions.** Is `endDayNow → STOPPED` intended (end-of-day = stop for the day), or
a mislabel/miswire of the 'pause' action? Should there be a first-class **Pause**
control that maps to desired-state `pause`? Was driving pause/resume via the
endpoint (bypassing the buttons) the right adaptation, or should the fix have been
UI-side?

---

## Problem 4 — Container↔host-daemon link ~40% unreachable

**Symptom.** From the data-plane container, ~40% of requests to
`http://host.containers.internal:8765/instances` failed ("host daemon unreachable"),
while the host daemon was **idle (0% CPU)** and **100% reachable directly from the
host** (5/5 sub-ms 401s). Started mid-session. Effect: cosmetic (durable operations
still work) but the per-bot cockpit intermittently shows a healthy running bot as
"resting / HOST_SERVICE_OFFLINE", hiding its on-duty controls.

**Solution applied.** Restarted the data-plane container (bots are **host**
processes, so untouched) to reset its connection state → **did not fix it** (so it's
a Podman host-gateway flap, not a container connection-pool issue). Worked around by
driving operations via endpoints (Problem 3) instead of gambling on the flaky
display.

**Verify / critique against:** `app/engine/live/host_daemon_client.py`, compose
`extra_hosts: host.containers.internal:host-gateway` for `python-service`.

**Questions.** What's the actual root cause of the ~40% flap, and the right fix
(retry/backoff in `host_daemon_client`, a keep-alive/pooled client, a unix-socket
or TCP-on-host-IP bridge instead of `host.containers.internal`)? Is the cockpit
correct to render a proof-plane blip as "bot offline/resting"?

---

## Cross-cutting decisions we specifically want judged

1. **Driving control mutations via the browser `fetch` to the app's own endpoints**
   (with `?control_intent=learn-ai-browser-control` so the dev proxy attaches the
   control secret) instead of clicking UI buttons. Justified adaptation given a
   broken pause button + flaky display, or scope-creep away from "UI-driven"?
2. **Hand-constructing the redeploy URL** with `parent_run_id` (Problem 2, step 3)
   rather than finding the in-app affordance that emits `freshRunRequested`.
   Legitimate use of the app's real mechanism, or a hack?
3. **`optional` everywhere** as the deploy policy (Problem 1). Correct, or should
   restarts have used `require`/`disabled` deliberately?
4. **Recording progress as ~13 doc commits on `master`** to keep the tree clean for
   the deploy page. Reasonable, or is there a cleaner way to satisfy the clean-tree
   gate?
5. **Effort/latency:** deep root-causing (reading logs, run artifacts, backend +
   frontend code) before escalating. Did this over-invest where a faster path
   existed?
6. **Leftover state:** two deadlocked-STOPPED instances (`nvda-0723`, `nvda-0723r`)
   and one crashed sick-bay bot (`spy-canary-0723`) left as records because "Take
   off roster" didn't remove them. Is there a correct cleanup/retire path?

---

## The direct ask for Codex

For each of Problems 1–4 and each cross-cutting decision: **(a)** confirm or refute
the root-cause claim against the cited code; **(b)** say whether the chosen
solution/workaround was the best available; **(c)** if not, give the better
solution. We especially want a verdict on **Problem 2**: is the STOPPED→restart
deadlock real and unavoidable in the current design, or is there a sanctioned
in-place restart path we missed? Rank the product findings by whether they're true
bugs vs. operator error vs. acceptable-by-design.
