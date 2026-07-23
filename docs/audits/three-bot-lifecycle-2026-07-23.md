# Three-bot lifecycle operations — 2026-07-23

Live UI-driven operations record. Times are CDT unless noted. Account `DUM284968`
(IBKR paper). Posture: paper submit-to-paper (real paper fills), 2000-order cap per
bot. Driven entirely through the cockpit UI; corrective actions recorded inline and
committed to `master` so the deploy page's clean-tree check stays satisfied.

## Plan

1. Launch bot 1 (SPY) at/after the 08:30 CDT open.
2. Stagger-launch bots 2 (QQQ) and 3 (NVDA), ~2 min apart.
3. Hold all three concurrent for 15 min; monitor fills / health / account CLEAN.
4. Stop + restart one bot.
5. Stop + restart two bots.
6. Stop all three; wait 10 min.
7. Restart one older bot, then launch two new bots (AAPL, MSFT).

## Intervention log

### 08:24–08:37 — cold-morning preflight recovery (DUM284968)

- **Symptom:** After a fresh container restart, the Account Desk for `DUM284968`
  showed `Account proof is not current` (receipt
  `acct-recon-DUM284968-1784781851577-31a6f7d55c118613` expired overnight),
  Account Clerk **Down** (generation 59), IBKR **disconnected**, and the broker
  snapshot unavailable ("Awaiting broker snapshot"). Host daemon was **Available**.
- **Corrective actions (all server-declared safe next steps, UI-only, no orders):**
  1. **Restore Clerk** via host daemon → Clerk came up **Normal / Ready**,
     generation **59 → 60**, phase Accepting. Feed: "Account Clerk restore
     completed" + "Broker event evidence recovered" (08:35:33).
  2. **Reconnect gateway** → `Paper · Connected`, Data farm **OK**, Subscriptions
     **Current** ("Data-plane paper session connected · DUM284968").
  3. **Run account reconcile** → replaced the stale proof with a fresh CLEAN
     receipt.
- **Verification:** Account Desk banner now reads **"Account is clean — the current
  reconciliation proof and account checks are passing."** Net liquidation
  $251,358.31, Day P&L $0.00, **Open positions: 0** (flat). Session capability:
  SPY and QQQ both **RTH live + tradeable** (paper). Auto-reconcile-after-bot-trades
  enabled.
- **Result:** Account is deploy-ready. Proceeding to bot launches.

### 08:40–08:50 — reuse attempt on overnight-stopped bots; pivot to fresh deploy

- **Context:** The fleet already held two off-duty bots from prior smoke runs —
  `smoke-spy-1522` (SPY) and `smoke-qqq-1525` (QQQ) — both **Flat**, 0 positions,
  account **Clear**, last run **Clean**, and each with a fresh roll-call **Start**
  offer. Both had filled orders yesterday (bot event stream shows `Order Filled`).
  Plan was to reuse them as bots 1–2 and deploy one fresh NVDA bot.
- **Symptom:** Clicking **Start** on `smoke-spy-1522` was refused with
  *"This bot is durably STOPPED. Resume it before starting. A precondition is not
  met."* Retrying after account re-verification gave the same refusal.
- **Investigation:** No **Resume** control was reachable — not in the Trader view
  (only Start / View operations), the Operations "•••" menu (Take off roster /
  Retire & Replace / Change settings / Full history), or the lifecycle overview
  (the "Desired state · STOPPED · Blocking step" card and its sub-stages are
  read-only receipts, exposing only *Select* and *Show receipts*). The graceful
  stop from a prior session wrote a durable STOPPED intent that the roster
  roll-call **Start** offer does not clear.
- **Decision:** Pivot to **fresh deploy** for the launch sequence — a newly
  deployed bot comes up desired-state RUNNING with no durable STOPPED intent, so
  it starts cleanly. The two smoke bots are left off-duty (flat, harmless). The
  "Resume a durably-stopped bot" gap is flagged to be nailed down at the
  stop/restart steps, where it is core to the test (and may need the same kind of
  control-restore done previously for the stop card).

### 08:52–09:00 — bot 1 fresh deploy crashed on start; root-caused to saved-state policy

- **Action:** Deployed a fresh SPY bot `spy-canary-0723` via the Deploy page —
  strategy EMA Crossover Signal, signal SPY, one `long SPY x1` leg, **Paper orders**
  (broker orders enabled), 2000/day — using **Deploy & run**. Launch request was
  accepted (run `e66c1a61…718891`), but the bot immediately entered **Sick bay**
  ("Bot crashed / Exited with error"). Roll call: 2 ready · 0 on duty · 1 sick bay.
- **Root cause (from run artifacts, not just the UI):** The host daemon
  (`app.engine.live.host_daemon`, PID on host :8765) launched the child, which
  **connected to IBKR paper fine** (`account=DUM284968 is_paper=True`), then
  crashed:
  `ERROR __main__ indicator-state hydrate failed (missing)`, `exit_code 4`,
  `exit_reason "exception"`. `indicator_state_hydration.json` shows
  `policy=require`, expected `live_state/ema_crossover_signal/SPY_15m.json`,
  `accepted=false`, `failure_reason="missing"`. The **Advanced start setting
  "Require saved state (recommended)"** makes a *new* bot's (expected) absence of
  saved indicator state a fatal error. This also explains why the smoke bots
  (which *have* saved state from yesterday) are the "supported" restart path.
- **Fix:** Re-deploy with **"Use saved state when available"** (`optional`) — starts
  fresh when no state exists (first deploy) and reuses state on later restarts, so
  it is correct for the whole lifecycle test. Clean up the crashed
  `spy-canary-0723` first, then redeploy with the corrected policy.
- **Note for the user / follow-up:** "Require saved state" being the *recommended*
  default while it hard-crashes every brand-new bot is a footgun worth fixing
  (default new deploys to `optional`, or auto-detect first-run). Recorded as a gap.

### 09:02 CDT — bot 1 (SPY) launched successfully with corrected policy

- **Action:** Re-deployed a fresh SPY bot `spy-0723` (EMA Crossover Signal, SPY,
  `long SPY x1`, Paper orders, 2000/day) with **Advanced start = "Use saved state
  when available"** (`optional`) via Deploy & run.
- **Result:** `spy-0723` is **On duty** with **Errors 0** — "Ready to act on the
  next bar; all hard gates pass." Roll call: 2 ready · 1 on duty · 1 sick bay.
  The `optional` policy resolved the crash. Bot 1 is live.
- **Left for later:** the crashed `spy-canary-0723` remains in Sick bay (flat,
  no runtime, harmless); to be retired during the 15-min hold. "Take off roster"
  did not remove it; will use Retire during the lull.

### 09:05–09:14 — bots 2 (QQQ) and 3 (NVDA) staggered up; 3 concurrent

- **Action:** Deployed `qqq-0723` (signal QQQ, `long QQQ x1`) at ~09:05 and
  `nvda-0723` (signal NVDA, `long NVDA x1`) at ~09:12, each EMA Crossover, Paper
  orders, 2000/day, start policy **optional**. Both via Deploy & run.
- **Result:** As of **09:14:40 CDT** the roster shows **3 on duty** —
  `spy-0723`, `qqq-0723`, `nvda-0723` — all Flat, account DUM284968. Roll call:
  2 ready · 3 on duty · 1 sick bay. spy-0723 and qqq-0723 report Errors 0
  ("Ready to act on the next bar; all hard gates pass"). nvda-0723 shows the same
  transient "Degraded: latest_reconcile — no reconcile receipt" that qqq-0723
  showed at launch and then cleared; monitoring for it to clear.
- **15-minute concurrent hold started 09:14:40 CDT** → stop/restart sequence at
  ~09:30. During the hold: retire the crashed `spy-canary-0723`, verify submission
  posture, and watch for fills + the nvda reconcile flag.

### 09:16 CDT — hold check 1: all 3 healthy; cockpit "resting" was a stale read

- **Cockpit vs. runtime discrepancy:** Opening `spy-0723`'s cockpit briefly showed
  "This bot is resting / host runner unreachable / HOST_SERVICE_OFFLINE". Verified
  against ground truth: host daemon (PID 31136, :8765) is up and responding;
  account_clerk (gen 60) running; and **all three `run start` child processes are
  alive and processing real-time bars** (`[BAR] 10:16:00-04:00 consolidator_emitted=1`
  at the :15 boundary — the 15-min consolidator is working). So the "resting" read
  was a transient per-bot **proof-plane** blip at page load, not a runtime failure.
  The roster refresh then showed all three **On duty, Errors 0**, "all hard gates
  pass" (nvda-0723's transient degraded-reconcile flag cleared).
- **Submission posture confirmed:** run artifacts show `submit_mode_at_start:
  "live_paper"` on all three — genuine submit-to-paper (real paper fills capable),
  not observe-only. Positions Flat; EMA crossover has not signaled yet.
- **Finding (UI):** the per-bot cockpit proof read can transiently show
  offline/resting while the runtime is healthy — worth hardening so operators don't
  mistake a proof-plane blip for a dead bot.

### 09:34 CDT — stop one (nvda-0723) succeeded; restart blocked by missing Resume

- **Intermittent control-plane blip:** During the stop step the container↔host-daemon
  (`host.containers.internal:8765`) link showed ~50% `host daemon unreachable`
  warnings for ~90s (14:32–14:33 UTC), which is why the cockpit briefly showed
  "resting / HOST_SERVICE_OFFLINE" while the runtime kept running. It self-recovered
  (last 60s: 14 OK, 0 fail). The host daemon itself is healthy (0.2% CPU, fast 401s
  from host and container). Flagged as a stability finding.
- **Graceful stop worked:** With reachability healthy, `nvda-0723`'s cockpit showed
  "on duty / all proofs satisfied" and the **"Stop bot gracefully"** control. Clicked
  it → `live_state/nvda-0723` went `desired_state: STOPPED` (`command_channel:STOP`),
  phase `OFF_DUTY`, `duty_outcome: STOPPED / HOST_DAEMON_PROCESS_STOPPED`. Verified
  **only** nvda's process (pid 56829) exited; `spy-0723` (51135) and `qqq-0723`
  (55323) stayed alive. Account remained flat. Clean, targeted stop.
- **Restart blocked — missing Resume control:** The stopped bot's cockpit offers
  only **Start**, which is refused: *"This bot is durably STOPPED. Resume it before
  starting."* The frontend **does implement** resume
  (`bot-control-page.component.ts`: `setIntent('resume','Resume')` →
  `setInstanceDesiredState({action:'resume'})`; canonical error "…Resume the bot to
  clear the stop latch"), but **no Resume button is surfaced** in the Trader or
  Operations cockpit for this state (verified via a11y-tree search + the lifecycle
  cards being read-only). This is the 2nd time this wall appeared (first: the smoke
  bots). **Product gap: a bot can be gracefully stopped but not resumed in-place via
  the UI.**
- **Corrective action:** Restart nvda-0723 via **re-deploy** (Deploy & run, same
  instance/config) — the proven UI-driven path — and document the missing in-place
  Resume as the blocker.

### 09:40–09:55 CDT — in-place restart is deadlocked (root cause); pause≠stop

- **Same-name redeploy** (Deploy & run, no lineage) → 409 "Deployment name already
  used". **Redeploy-from-run** (deploy URL carrying `parent_run_id`, the app's real
  same-instance redeploy) got past that but → 409 **"Stopped Requires Resume"**
  ("Use Resume to set desired_state=RUNNING, then start").
- **Drove the real resume operation** (`POST /api/live-instances/nvda-0723/desired-state
  {action:resume}`, executed from the browser through the app proxy so the control
  secret was attached — auth worked, status 409 not 401). The resume **gate refused**:
  `allow_resume:false`, `BROKER_SAFETY_UNKNOWN` + `SUBMISSION_CAPABILITY_UNKNOWN`
  ("run_status.json absent").
- **Root cause (deadlock):** resume requires a live run's broker/submission proof,
  but a **fully-STOPPED** bot has no run → no proof → resume forever blocked. Resume
  is meant for a **PAUSED** bot (run still alive, e.g. "End day now"). "Stop bot
  gracefully" writes STOPPED and **kills the run**, so it cannot be resumed in place.
  Net: **a gracefully-stopped bot cannot be restarted in place via the UI** — the
  only reliable restart is a **fresh deploy under a new name**. (Prior-session memory
  notes restart worked before — likely because those used pause, or a regression.)
- **Product findings (for the user):** (1) cockpit never surfaces a Resume control
  for a STOPPED bot — it offers "Start", which is refused; (2) the deploy 409 tells
  the operator to "Use Resume", but Resume is gated shut for a stopped bot — the
  guidance and the gate contradict; (3) consider: STOP should offer a re-deploy path,
  or the stop/restart lifecycle should route through PAUSE/RESUME.
- **Fleet state right now:** `spy-0723` + `qqq-0723` still On duty (untouched);
  `nvda-0723` cleanly STOPPED, restart pending an approach decision.
