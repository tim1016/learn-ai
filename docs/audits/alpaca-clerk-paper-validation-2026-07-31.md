# Alpaca Clerk paper validation — 2026-07-31

**Status:** complete; deploy, churn, fault injection, restart recovery,
market-hours fills, and Clerk-led terminal wind-down are proven.

**Issue:** [#1324](https://github.com/tim1016/learn-ai/issues/1324)

**Account:** `PA3KWXU1C4C3` (`paper`)

**Branch:** `codex/issue-1324-paper-deploy`

**Base:** `ea68e980bed12aa76dd7b582cf27b35812029f58`

## Evidence rules

- Browser navigation and production panel controls are the only mutation path
  used for deploy, lifecycle actions, restart recovery, and wind-down.
- Read-only local artifacts, service logs, and API projections may corroborate
  UI receipts.
- A bot is counted as working only when the production roster says `Working`.
- A trade is counted only from durable Clerk order/fill evidence attributed to
  that strategy-instance namespace.
- The verdict is final only because the market-hours exercise ended with zero
  running bots, zero positions, zero open orders, and clean reconciliation.

## Acceptance-criteria ledger

| Criterion | Evidence | State |
| --- | --- | --- |
| Closed Alpaca paper deploy workflow | The production dialog exposes only `deployment_validation`, one STK entry/close plan, explicit paper account, `safe_canary` or bounded custom shares | Proven |
| Single Clerk-governed deploy with authored copy | Backend response owns eligibility, receipt, explanation, next action, and action plan; Angular renders generated contract types | Proven |
| Paper-only at submission boundary | Broker execution rejects a non-paper account independently of the UI | Proven by regression |
| Authored production details | Trader and Operator lenses rendered backend-authored lifecycle, custody, reconciliation, freeze, channel, and pipeline copy | Proven |
| Regenerated contract/types | OpenAPI check and Angular generated-type check pass; no TypeScript-owned semantic fixture contract was introduced | Proven |
| Fault injection | Eleven deterministic cases cover rejection, uncertainty, cancellation race, partial-fill restart, crash/restart, approved/forbidden/mismatched carryover, account-policy revocation, and both freeze categories | Proven |
| Five-always-running churn | Fifteen panel actions, never below five Working, restored to eight | Proven |
| Eight-bot market exercise and panel-led wind-down | Eight trade-mode bots remained concurrently Working for more than 15 minutes; all 30 submitted orders filled, every bot completed at least one buy/sell cycle, and panel actions ended all eight `STOPPED / STOPPED_FLAT` | Proven |
| Final audit totals and verdict | This document records durable per-bot order/fill totals, terminal lifecycle receipts, the final production account projection, live defects, and deviations | Proven |

## Implementation ledger

| Commit | Subject |
| --- | --- |
| `00ef3060` | Closed Alpaca paper deploy workflow and generated contract |
| `288a42eb` | Panel recovery and Resume custody |
| `aba2b2cb` | Exact stopped-carryover custody and authored production details |
| `0792e6f6` | Truthful empty LIVE chart before the session opens |
| `e9753905` | Legal panel Resume after interrupted restart |
| `bb1bd7e4` | Preserve existing panel formatting around the chart regression |
| `b7f7baad` | Premarket validation evidence |
| `dba8aa6c` | Normalized audit Markdown |

## Account preflight

The production roster reported:

- account `PA3KWXU1C4C3`, mode `PAPER`;
- equity and cash `$99,987.12`;
- buying power `$399,948.48`;
- reconciliation `Clean`;
- market-data and execution channels `Healthy`;
- no displayed bot-attributed exposure before the session.

The data plane also reported its IBKR paper market-data session connected to
`DUM284968`. Alpaca remains the execution broker; IBKR supplies the strategy's
market data for this phase.

## Eight-bot fleet

Every deployment used the production UI, trade mode, quantity `1`, and
per-deploy carryover policy `FORBID`.

| SID | Symbol | Initial run | First recovered run | Market run |
| --- | --- | --- | --- | --- |
| `alp1324-aapl-0731` | AAPL | `db60a866d2b34154b33f07bd76627aee` | `0ea422fc6c2849c79bc424f1d6158319` | `22cebd89e1044038b148a1dd0bcf63bf` |
| `alp1324-amd-0731` | AMD | `228227058ccc48e3aab2239aa7806a87` | `097cc2a2ed9a4f97bbf6e31003adac54` | `30a72f7c3b9048038450b270bc1b793a` |
| `alp1324-amzn-0731` | AMZN | `174f481c0ffd48cbb473390034899f3b` | `b1ae75bf05c640eab1ddd2e8f22d47a2` | `1dd2c877a2d74e6e9189e8a59076b4c7` |
| `alp1324-meta-0731` | META | `7519cdfd22ca4507bfc2ca747a1cb5ed` | `38f94e78072d40899f970a0d99e12342` | `1efef5e1641148d085e593f4eb4f548b` |
| `alp1324-msft-0731` | MSFT | `4242a30a186948ef96dbf214b020f4b2` | `33d311fe00ab46d794cb5ace8199d1cf` | `d57ff2f8d8544a5a9d453bdcdefbb849` |
| `alp1324-nvda-0731` | NVDA | `074fd97da2734ae3a9e663b806c52e19` | `2aae87ccffb645de9ef139d75700520c` | `d7c5e2ffa3de4a128057017a390fd9f5` |
| `alp1324-qqq-0731` | QQQ | `6fcf2a98490e4ca7af8ca31b8d0dac5c` | `b4fa7e3d0b9c4271872d0f5b2b2a4ddd` | `393b9d5f1dc144289ef71a57bb1b32fb` |
| `alp1324-spy-0731` | SPY | `4d36474c49d844c6b1fc388e794292e4` | `690d1308ee8a440da60300dcdd7369f7` | `09a4a99ec2f04736bd15061232935350` |

The roster showed all eight market-run instances as `Working` by 08:42:41
CDT. They remained concurrently Working beyond 08:57:41, proving the required
15-minute interval, and every instance subsequently produced attributed paper
fills.

## Five-always-running churn drill

Starting from eight Working bots, the operator used row actions in this order:

1. Stop AMD, stop AMZN — fleet floor reached five because AAPL was already
   stopped for the initial control sample.
2. Start AAPL, stop META.
3. Start AMD, stop MSFT.
4. Start AMZN, stop NVDA.
5. Start META, stop QQQ.
6. Start MSFT, stop SPY.
7. Start NVDA, start QQQ, start SPY.

The roster never displayed fewer than five Working bots and ended with all
eight Working. All fifteen action requests completed successfully; no stale
action returned 409.

Observed UI latency across the fifteen actions:

| Statistic | Milliseconds |
| --- | ---: |
| Minimum | 9,404 |
| Median | 14,531 |
| Mean | 14,493 |
| Maximum | 18,561 |

Reconciliation remained clean throughout the drill.

## Restart evidence

The Python data-plane service was restarted with all eight bots on duty.
Boot recovery:

1. did not auto-restart an interrupted task;
2. recorded `EXITED_UNVERIFIED / INTERRUPTED_BY_RESTART`;
3. reconciled the account without duplicating an order;
4. durably transitioned interrupted desired state to `STOPPED`;
5. exposed an enabled, proof-gated Start action;
6. launched eight new run IDs only after the operator used that panel action.

The live exercise found and fixed a pre-existing stranded-state defect: the
older boot sweep recorded the interrupted lifecycle outcome but left desired
state as `RUNNING`, while Resume admits only `STOPPED`. `e9753905` adds both
the correct transition and a boot-time repair for already-stranded artifacts.
The production fleet then resumed entirely through panel controls.

The market-day restart exposed the broader form of the same invariant. AAPL
encountered `FEED_DEATH` while the IBKR data plane was unavailable and became
`OFF_DUTY / CRASHED` while desired state still said `RUNNING`. The regression
fix now fail-closes every unexpected terminal exit to durable `STOPPED` before
recording its lifecycle outcome. Boot recovery also repairs any terminal
off-duty artifact that still says `RUNNING`, including a graceful
`SERVICE_SHUTDOWN`. Focused tests were written red first, then the full
bot-runner and boot-recovery scope passed 42/42. After the service restart,
the operator launched all eight market runs through the production panel; no
task auto-restarted and no order was duplicated.

## Fault-injection matrix

| Scenario | Required invariant | Result |
| --- | --- | --- |
| Broker rejection | Durable terminal rejection; no invented fill | Pass |
| Outcome uncertainty | Durable uncertain intent; no blind duplicate submit | Pass |
| Cancellation race | Cancel and acknowledgement converge through Clerk truth | Pass |
| Partial-fill restart | Filled quantity survives recovery and remains attributed | Pass |
| Crash/restart | Interrupted run is honest, not auto-restarted, no duplicate order | Pass |
| Approved carryover | Exact checkpoint plus fresh proof admits Resume | Pass |
| Carryover mismatch | Changed attributed exposure refuses Resume | Pass |
| Carryover forbidden | Per-deploy `FORBID` refuses exposed Resume | Pass |
| Account policy revoked | Account opt-in is rechecked at Resume | Pass |
| Unattributable account state | Freeze is exactly `ACCOUNT_STATE_UNATTRIBUTABLE` | Pass |
| Unprovable account state | Freeze is exactly `ACCOUNT_STATE_UNPROVABLE` | Pass |

Result: **11/11 passed**.

## Live defects found during validation

### Premarket LIVE chart returned 500

The production LIVE chart passed `session_open → now` to the range resolver
before the opening bell, so the end preceded the start. `0792e6f6` returns a
truthful empty session chart pre-open. The regression suite passed, and the
signed-in panel subsequently rendered the LIVE chart while the service logged
HTTP 200 responses.

### Interrupted bots had no legal panel recovery action

The boot sweep left desired state `RUNNING` after marking the task interrupted.
The panel correctly requires `STOPPED` before Resume, so Start was disabled.
`e9753905` makes restart recovery a durable fail-closed STOPPED transition and
repairs artifacts produced by the earlier behavior. Focused bot-runner,
boot-recovery, and panel tests passed; the production panel then resumed all
eight bots.

### Unexpected terminal exits could strand desired state

During the market-day service start, AAPL's data feed died before IBKR Gateway
was available. Lifecycle truth was correctly `OFF_DUTY / CRASHED / FEED_DEATH`,
but desired state remained `RUNNING`, making the proof-gated Start action
illegal. The final bot-runner change persists `STOPPED` for `CRASHED` and
`EXITED_UNVERIFIED` outcomes and generalizes boot repair to every terminal
off-duty artifact with lingering `RUNNING` intent. Regression coverage proves
runtime exception, feed death, cancellation without stop intent, finite stream
end, interrupted restart, and graceful service-shutdown repair.

### Reconciliation briefly froze on a submission race

At 08:58:24 CDT the Operator lens honestly raised
`ACCOUNT_STATE_UNATTRIBUTABLE / missing_intent` while a newly observed broker
fact had not yet joined its Clerk intent. A panel-led reconciliation at
08:58:58 still reported the freeze. Entry remained fail-closed while an already
owned protective sell was allowed to reduce risk. Durable order events caught
up, the next sweep was clean at 09:00:41, the account freeze disappeared, and
uncertain intents returned to zero. No bypass or direct runner action was used.

## Validation receipts

- Changed Alpaca Clerk, v2 panel, bot-runner, and boot-recovery scope:
  373 passed.
- Broader Alpaca, lifecycle, panel, runner, and engine scope: 579 passed in
  one invocation. Its sole local failure was the negative missing-credentials
  test reading the developer `.env`; the same test passed from a clean working
  directory with only a dummy Polygon test value, proving the CI-shaped
  credential-isolation path.
- Red/green terminal-state regression scope: 42 passed after the expected red
  assertions against lingering `RUNNING` state.
- Post-review action-execution, panel, runner, and recovery scope: 63 passed.
- Angular: 238 files, 1,853 tests passed, including the frontend semantic
  literal guard.
- Ruff: full application and test scopes passed.
- OpenAPI snapshot check passed.
- Angular GraphQL/OpenAPI generated-type check passed with no diff.
- Application and spec TypeScript `--noEmit` checks passed.
- Full v2-panel ESLint scope passed with zero warnings.
- `git diff --check` passed.

## Market-hours order and fill ledger

| SID | Submitted orders | Filled orders | Buy fills | Sell fills | Terminal exposure |
| --- | ---: | ---: | ---: | ---: | ---: |
| `alp1324-aapl-0731` | 4 | 4 | 2 | 2 | 0 |
| `alp1324-amd-0731` | 2 | 2 | 1 | 1 | 0 |
| `alp1324-amzn-0731` | 6 | 6 | 3 | 3 | 0 |
| `alp1324-meta-0731` | 4 | 4 | 2 | 2 | 0 |
| `alp1324-msft-0731` | 6 | 6 | 3 | 3 | 0 |
| `alp1324-nvda-0731` | 4 | 4 | 2 | 2 | 0 |
| `alp1324-qqq-0731` | 2 | 2 | 1 | 1 | 0 |
| `alp1324-spy-0731` | 2 | 2 | 1 | 1 | 0 |
| **Total** | **30** | **30** | **15** | **15** | **0** |

Counts come from unique durable Clerk `submit_acked` intent IDs and unique
broker order IDs with terminal `filled` events. The production roster agreed
with the ledger: each bot showed the same fill count, zero open P&L, and no
exposure before Stop.

## Terminal wind-down

The wind-down used production panel controls only. At the first wind-down
snapshot, AAPL, AMD, MSFT, NVDA, QQQ, and SPY had attributed exposure while
AMZN and META were flat. The strategy's already-owned EXIT operations then
flattened every exposed bot. AAPL's `Flatten & stop` request raced its terminal
sell fill and returned a safe 409 against the changed action revision; after a
fresh panel projection AAPL was proven flat and the roster Stop succeeded.
No retry loop or direct runner route was used.

The operator then selected Stop for each of the eight proven-flat rows. Durable
lifecycle state recorded `STOPPED / STOPPED_FLAT` for every market run between
09:04:39 and 09:05:08 CDT. The final production projections proved:

- eight of eight target bots `Off duty`;
- zero attributed exposure in every row;
- no open positions, with long and short market values both `$0.00`;
- no open orders; every displayed market-day order was terminal `Filled`;
- clean reconciliation, no account hold or freeze, zero uncertain intents,
  and healthy current market-data and execution channels.

## Thermo-nuclear review

Exactly one thermo-nuclear code-quality review was run after implementation,
live validation, terminal wind-down, and this audit were complete, immediately
before publication.

The review found one blocker: `Flatten & stop` asked the Clerk to EXIT before
stopping strategy evaluation, and its `UNPROVABLE` branch returned early while
claiming the bot was stopped. The remedy orders custody as STOP first, then
EXIT, so a failed flatten cannot leave the strategy running or able to re-enter.
A regression failed against the old ordering and the remediated 63-test scope
passed.

The sole non-blocking maintainability finding is that `bot_runner.py` reached
exactly 1,000 lines despite the correct carryover-policy extraction. It is
captured as [#1331](https://github.com/tim1016/learn-ai/issues/1331) with a
behavior-preserving decomposition boundary; it is intentionally not expanded
into this final stack PR. No second review cycle was run.

## Deviations

- No direct runner-route mutation has been used.
- Three live defects were fixed and regression-tested. The transient
  reconciliation freeze was exercised and cleared through the panel without a
  safety bypass.
- One `Flatten & stop` action returned a truthful revision 409 because the
  owned exit filled between projection and action. A fresh projection proved
  the bot flat, after which Stop completed normally.
- The broad local Python suite has environment-specific blockers documented
  above. This audit does not relabel those runs as green.

## Verdict

**Pass.** The first usable Alpaca paper workflow is complete. All eight bots
ran concurrently, all 30 Clerk-owned orders filled, every bot completed at
least one entry/exit cycle, reconciliation recovered without bypass, and the
panel-led wind-down ended with no runtime, order, position, or custody residue.
