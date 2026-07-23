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
