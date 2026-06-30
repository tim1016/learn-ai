# Deployment Validation Redeploy - 2026-06-30

Purpose: redeploy three paper-trading deployment-validation bots and keep
iterating until each scenario reaches a running state with incoming and
outgoing paper trades.

## Scope

- Paper account: `DUM284968`
- Strategy: `deployment_validation`
- Sizing: `FixedShares(1)`
- Hydration: `optional`
- Clean-tree caveat: ledgers were minted with `clean_tree_scope=("references/qc-shadow",)`
  because this validation pass intentionally exercised local uncommitted runtime
  changes for cross-asset action-plan consumption.

## Target Scenarios

| Scenario | Successful strategy_instance_id | Run ID | Result |
| --- | --- | --- | --- |
| SPY signal -> SPY asset | `DVS-SPY-SPY-0630` | `f9187f2f0d6763a7ec831f385a9035d25a7c207fae832db857cfb8e1b42da2a7` | Running, BUY 1 SPY then SELL 1 SPY |
| SPY signal -> NVDA asset | `DVS-SPY-NVDA-0630` | `27c58eabc4af74dbff7255ea0055a30f819bfc6e835f2463cfd64ed950388fdc` | Running, BUY 1 NVDA then SELL 1 NVDA |
| NVDA signal -> SPY asset | `DVS-NVDA-SPY-0630` | `90e113a20e98eb82fa628350b06c2775978c21161e78fdd6bdd9c4491add927b` | Running, BUY 1 SPY then SELL 1 SPY |

## Execution Evidence

SPY -> SPY:

- `executions.parquet`: `SPY +1 @ 747.76`, `SPY -1 @ 747.53`
- Signal sequence: `ENTER` at `1782847440000`, `EXIT` at `1782847620000`

SPY -> NVDA:

- `executions.parquet`: `NVDA +1 @ 199.20`, `NVDA -1 @ 199.11`
- Signal sequence: `ENTER` at `1782847440000`, `EXIT` at `1782847620000`

NVDA -> SPY:

- `executions.parquet`: `SPY +1 @ 747.76`, `SPY -1 @ 747.50`
- Signal sequence: `ENTER` at `1782847440000`, `EXIT` at `1782847620000`

## Failures And Fixes

1. Stale host daemon
   - Symptom: `/health` reported `code_stale=true`.
   - Action: restarted the host daemon on repo head `99262ad18312fc6ad4d8182205fb232e2c62fd4f`.

2. Action plan was declarative only for cross-asset runs
   - Symptom: existing docs and runtime treated `live_config.action` as identity,
     not the traded asset.
   - Action: added deployment-validation `trade_symbol` support and mapped a
     single long stock action leg to that param at live start.

3. Restart-intensity freeze
   - Symptom: launching three starts inside five minutes froze the account:
     `restart_intensity.threshold_breached:observed=3:threshold=3`.
   - Action: fetched IBKR paper positions with diagnostic client id `90`,
     confirmed `positions=[]`, and cleared the freeze with recovery proof
     `restart-freeze-clear-c8cf1ff4ffda`.
   - Improvement: the cockpit should present a countdown and queue launches
     instead of letting the third start write an ACTIVE binding that freezes the
     account.

4. IBKR orderRef length cap
   - Symptom: long strategy instance IDs failed closed before broker submit:
     `OrderRefTooLongError: order_ref length 71/72 exceeds cap 60`.
   - Action: relaunched with compact IDs:
     `DVS-SPY-SPY-0630`, `DVS-SPY-NVDA-0630`, `DVS-NVDA-SPY-0630`.
   - Improvement: deploy validation should enforce the orderRef budget before a
     run is minted or started.

5. Decision parquet OHLC nulls were a false lead
   - Symptom: `bar_open/bar_high/bar_low` were null in `decisions.parquet`.
   - Finding: decision rows intentionally only include the strategy snapshot
     price today; live `TradeBar` still carried enough data for the strategy.
   - Improvement: make this clearer in operator tooling or populate OHLC when
     available to avoid misdiagnosis.

6. Bots page rendered empty after the successful relaunch
   - Symptom: `http://localhost:4200/broker/bots` showed `0 bots` even though
     the host daemon still listed the active `DVS-*` processes.
   - Root cause: after restarting the wedged Python data service, the catalog
     endpoint returned HTTP 500 because `SizingAuditRow.reference_price`
     required `str`; FixedShares audit rows can legitimately carry
     `reference_price=null` when sizing does not need a bar price.
   - Action: made the backend and frontend audit-row contracts nullable for
     `reference_price`, added a sidecar fallback regression test, restarted
     `polygon-data-service`, and verified `/api/live-instances/catalog` plus
     the Angular page both show the `DVS-*` bots.
   - Improvement: fleet catalog should degrade one malformed/audited row
     without hiding the whole bot fleet.

## Follow-Up Candidates

- Enforce `strategy_instance_id` length against `order_ref` before deploy/start.
- Surface restart-intensity remaining cooldown in the cockpit.
- Show the delayed-order countdown/queue state in the cockpit while waiting for
  next-bar fills.
- Enforce the broker `orderRef` budget at ledger/deploy mint time.
- Make action-plan trade targets first-class in the live preflight/operator
  surface, not only strategy-param plumbing.
- Decide whether deployment specs should carry both signal symbol and trade
  symbol explicitly, rather than deriving trade symbol from `live_config.action`.
- Harden the catalog projection so one invalid instance row becomes an
  instance-level warning rather than a fleet-wide empty page.
