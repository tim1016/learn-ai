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
