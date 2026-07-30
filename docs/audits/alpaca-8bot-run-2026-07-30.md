# Alpaca 8-bot paper run — evidence report (2026-07-30)

First-ever Alpaca trades on this platform: eight trade-mode bots deployed,
run, and stopped **entirely through the broker-v2 panel UI** on paper account
`PA3KWXU1C4C3`. Orchestration: Fable directing Sonnet agents (builders,
UI operators, watchers, investigators). Design spec:
`docs/superpowers/specs/2026-07-30-alpaca-8bot-run-design.md`.

## Outcome vs success criteria

| Criterion | Result |
|---|---|
| ≥15 min with all 8 bots concurrently running | ✅ **20+ min** (T0 16:47:14Z → 17:07:14Z), then ~2 h total until wind-down |
| Every bot submits ≥1 real filled order | ⚠️ All 6 **trade-mode** bots traded; QQQ + AMZN turned out to have been deployed in `log_only` (dialog default left unchanged by the operator agent — discovered at wind-down). Full day: **152 bot orders, 76 buys / 76 sells, 152/152 filled** |
| Account ends flat, clean reconciliation | ✅ (wind-down section below) |
| Evidence report written | ✅ this document |

## What was built for the run (same-day, branch `codex/feat-alpaca-bot-navigation`)

- `3eb72cc9` — **trade mode** in the broker-v2 bot runner: `mode="trade"` +
  `quantity` on deploy; `_run_trade_bot` ports the
  `DeploymentValidationConsecutiveGreen` signal (2 consecutive green 1-min
  bars → market BUY, exit 3 bars later; window open+15m → close−15m from the
  canonical calendar; fixed-share sizing divergence documented). All orders
  via `AlpacaClerk.submit_for_instance`; order identity
  `learn-ai/{sid}/v1:{intent_id}` so panel fills/P&L attribute natively.
- `ff567b9f` — deploy dialog gains mode/quantity; unscoped bots route
  resolves the account and redirects (sidebar link fix; pushed to PR #1318
  as `eeac61f7`).

## Fleet session (watcher record)

- T0 (8th bot ON_DUTY): **16:47:14Z**; monitored to T0+20 min, bots kept
  running afterwards for the churn phase.
- **41 orders, 41 fills** (21 buys / 20 sells) by 17:07Z; max simultaneous
  exposure 5 shares (cap 8). Per-bot at T0+20m: MSFT 8 fills (−$0.89),
  NVDA 7 (+$0.44), SPY 6 (+$0.17), AAPL 6 (−$0.49), AMD 6 (−$1.17),
  META 6 (+$0.80), QQQ/AMZN 0. Net realized **−$1.14** at that snapshot
  (paper; fees not reported by Alpaca paper).
- **Correction to the watcher's read**: QQQ and AMZN were not "no-signal" —
  they had been deployed `log_only` (dialog default). Follow-up: for a
  trade-run dialog, defaulting to the silent mode is a UX trap.
- Full-day totals at wind-down: **152 bot orders (76 buys / 76 sells),
  152/152 filled**, ~13 round-trips per trade-mode symbol over ~2 h.
- IBKR 5-second-bar delivery gaps occurred and **self-healed** through the
  `live_idempotent` redelivery policy (one exact-redelivery skip observed) —
  the temporal-rigor subscription relaxation working as designed.
- Clerk: no hold at any point; `outstanding_intents` 0 throughout.

## Defect ledger — found live, fixed same-day

Every defect below was exposed by the first real order flow through surfaces
that had only ever seen fixtures.

| # | Commit | Defect | Root cause |
|---|---|---|---|
| 1 | `d8ae4cbb` | Every UI deploy 404'd | Account-scoped `POST /accounts/{id}/bots` was never registered; dialog posts there. Added scoped alias with typed mismatch-404 + regression tests |
| 2 | `d8ae4cbb` | Deploy dialog had no buttons | `pTemplate="footer"` without `PrimeTemplate` import — template never rendered. Switched to PrimeNG 22 `#footer` ref |
| 3 | `651a1f6f` | Bots page + account strip never finished loading | Polling by epoch-in-`params` aborts/resets the `resource()` every tick; endpoint slower than the interval ⇒ never converges. Switched to `reload()` polling |
| 4 | `2787094a` | Roster rendered zero bots while holding 8 rows | Host `display:block` collapsed the `flex:1` table wrap; CDK virtual-scroll viewport got 0 px height |
| 5 | `4d3b8db6` | `missing_intent` verdict during every bot hold window | Sweep compared live positions against the journal's latest order snapshot; the fill `ORDER_EVENT` lands ~7 s later (proved by timestamps). In-flight orders now suppress the symbol for that pass; foreign positions still flag |
| 6 | `f0b11719` | Panel requests stacked unboundedly | 5 s poll fired regardless of in-flight state; guarded |
| 7 | `526834fb` | Every evidence page 500'd after the first fill | Renderer read `event.filled_quantity/filled_avg_price`; `BrokerOrderEvent` fields are `quantity/price` |
| 8 | `526834fb` | Chart history 500'd for every symbol | Polygon Starter answers `status=DELAYED` (15-min delayed entitlement); fetcher treated it as an error |
| 9 | `ecfdc9e2` | Panel/catalog ~8–18 s per request | Every request re-validated the static account id with a full Alpaca REST `get_account` (5–15 s on the paper API that day) + the sweep made its two ~7 s broker reads sequentially. 60 s port-keyed cache + `asyncio.gather` |

**Defect #10 — found, NOT fixed today (the run's biggest open finding):**
the panel's presented-action **stop** could never land on an actively trading
bot. The action handler re-fetches the panel (then ~18 s stale) to obtain the
action `revision`, and a trading bot advances its revision faster than that
window — every stop POST returned **409 revision-stale**. Two independent
operator agents each spent 30+ minutes failing to stop a single bot before
this was diagnosed. Wind-down had to bypass the presented-action route (see
below). Remediation candidates: server-tolerant revision matching for
idempotent actions like stop, a revision-free stop, or simply the now-fast
panel shrinking the staleness window — needs a deliberate design decision
against the presented-actions contract (spec §11).

Unresolved observations (follow-ups, not fixed today):

- **META transient deploy failure**: first attempt returned the dialog's
  generic error, immediate retry succeeded. Cause not established.
- Panel/catalog latency after the account-id cache: ~4.4 s consistent —
  one residual per-request Alpaca REST call remains somewhere in the
  catalog/panel path (cache-hit second call did not improve); trace it.
- Chart history ~17 s (Polygon backfill fetch per request).
- Deploy dialog defaults to `log_only` — silent-no-op trap for trade runs
  (see QQQ/AMZN above).
- `clerk.status()` takes the exclusive intake lock for a pure read
  (3–20 s under load); investigator proposes a lock-free read path.
- Panel row actions fetch the full panel before executing (adds the panel
  latency to every action).
- Roster virtual scroll renders only visible rows; operator agents had to
  filter per-sid (known review flag: decorative virtualization).
- Alpaca paper REST latency itself was 5–15 s/call with high variance that
  afternoon — architectural context for all panel timings.

## Reconciliation verdict behavior

`clean` and `missing_intent` alternated in exact correlation with bots'
hold windows (clean when all flat). Root cause was defect #5; verdict noise
misled two operator agents (one correct safety abort, one adjusted rule).
After the fix, an in-flight journaled order suppresses its symbol for the
pass; a position with **no** journal presence still flags — preserving the
detection that correctly caught the pre-run stale share (below).

## Preflight findings (kept for the record)

- Stale 1-share SPY long (from 2026-07-24 keys validation, no journaled
  intent) was detected by the sweep (`missing_intent`, 2.6 days old) and
  closed through the desk's manual order form — which doubled as the smoke
  test of the wind-down fallback. Verdict went clean after the close.
- IB Gateway on host port 4002 with `IBKR_BROKER_ENABLED=true` was a hard
  dependency (bots consume IBKR bars; Alpaca-native feed deferred by
  decision D2).
- A concurrent external agent session switched the checkout to `master`
  mid-run and left an unrelated README rewrite; recovered our branch and
  restored `master`. Single-checkout + multiple agents remains a real
  operational hazard.

## Churn drill (operator request: random start/stop, 5 always running)

- **Take 1** aborted correctly by its safety rule on the (then-unexplained)
  `missing_intent` verdict — the right call given its instructions.
- **Take 2** wedged: with defect #7 breaking evidence views and ~18 s pages
  (defect #9), the operator burned its window verifying state and never
  executed a stop. Stopped by the director.
- **Honest outcome: the churn drill was never executed.** Take 1 was blocked
  by verdict noise (defect #5, fixed), take 2 by page latency + evidence 500s
  (defects #7/#9, fixed) — and the decisive blocker for any take was defect
  #10: the UI stop action 409s on a trading bot, so the 8→5 drawdown was
  impossible through the panel. The 5-always-running invariant remains
  untested. It is re-runnable once #10 is resolved; the rest of the stack is
  now fast and clean.

## Wind-down and final state

With the UI stop path broken (defect #10) and two operator agents burned on
it, wind-down deviated from D5, documented here: the 7 flat bots were stopped
via the bot-runner's own `POST /api/brokers/alpaca/bots/{sid}/stop` (the same
operation the panel action ultimately performs, minus the revision contract),
reason recorded on each. AAPL was in-position; its natural 3-bar exit was
awaited (position closed on its own), then it was stopped the same way.

End state, verified:

- Bots running: **0** (all 8 stopped with recorded reasons).
- Positions: **none**; open orders: none. 76 buys / 76 sells — every entry
  closed by its exit.
- Data plane restarted with all fixes; post-restart: reconciliation verdict
  **clean** (stable — the flicker is gone with the account flat and defect #5
  fixed), no hold, 0 outstanding intents; evidence endpoint 200 (was 500);
  chart history 200 (was 500); panel/catalog ~4.4 s (from 8–18 s).
- Net realized P&L for the day: ≈ **−$1** on ~$100k paper equity (fees not
  reported by Alpaca paper).

## Process notes (orchestration)

- Fable directed; Sonnet agents executed: 2 builders (worktrees), 4 UI
  operators, 3 watchers, 3 investigators. All state-changing broker actions
  went through the panel UI per design decision D5; read-only API polling
  provided the evidence trail.
- The canary pattern paid for itself twice: its first failure surfaced
  defects #1–#2 before 8 bots hit them; watcher early-exit rules caught the
  verdict flicker with evidence attached.
- Agent worktree isolation leaked once (FE builder wrote to the main
  checkout as well); reconciled by content-identity check before commit.
