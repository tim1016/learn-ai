# Alpaca Clerk paper validation — 2026-07-31

**Status:** in progress; premarket deploy, churn, fault injection, and restart
recovery are complete. Market-hours fills and terminal wind-down remain pending.

**Issue:** [#1324](https://github.com/tim1016/learn-ai/issues/1324)  
**Account:** `PA3KWXU1C4C3` (`paper`)  
**Branch:** `codex/issue-1324-paper-deploy`  
**Base:** `979e3dca5e949e38bc2b8383bd2a0066d4a61605`

## Evidence rules

- Browser navigation and production panel controls are the only mutation path
  used for deploy, lifecycle actions, restart recovery, and wind-down.
- Read-only local artifacts, service logs, and API projections may corroborate
  UI receipts.
- A bot is counted as working only when the production roster says `Working`.
- A trade is counted only from durable Clerk order/fill evidence attributed to
  that strategy-instance namespace.
- The verdict stays pending until the market-hours exercise ends with zero
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
| Eight-bot market exercise and panel-led wind-down | Eight trade-mode bots are Working premarket; fills and wind-down are pending the regular session | Pending |
| Final audit totals and verdict | This document is the evidence ledger; terminal totals and verdict remain pending | Pending |

## Implementation ledger

| Commit | Subject |
| --- | --- |
| `c26790b8` | Closed Alpaca paper deploy workflow and generated contract |
| `cc0ac71e` | Panel recovery and Resume custody |
| `06386704` | Exact stopped-carryover custody and authored production details |
| `afde86ba` | Truthful empty LIVE chart before the session opens |
| `68373ea3` | Legal panel Resume after interrupted restart |
| `0642cdc9` | Preserve existing panel formatting around the chart regression |

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

| SID | Symbol | Initial run | Post-restart run |
| --- | --- | --- | --- |
| `alp1324-aapl-0731` | AAPL | `db60a866d2b34154b33f07bd76627aee` | `0ea422fc6c2849c79bc424f1d6158319` |
| `alp1324-amd-0731` | AMD | `228227058ccc48e3aab2239aa7806a87` | `097cc2a2ed9a4f97bbf6e31003adac54` |
| `alp1324-amzn-0731` | AMZN | `174f481c0ffd48cbb473390034899f3b` | `b1ae75bf05c640eab1ddd2e8f22d47a2` |
| `alp1324-meta-0731` | META | `7519cdfd22ca4507bfc2ca747a1cb5ed` | `38f94e78072d40899f970a0d99e12342` |
| `alp1324-msft-0731` | MSFT | `4242a30a186948ef96dbf214b020f4b2` | `33d311fe00ab46d794cb5ace8199d1cf` |
| `alp1324-nvda-0731` | NVDA | `074fd97da2734ae3a9e663b806c52e19` | `2aae87ccffb645de9ef139d75700520c` |
| `alp1324-qqq-0731` | QQQ | `6fcf2a98490e4ca7af8ca31b8d0dac5c` | `b4fa7e3d0b9c4271872d0f5b2b2a4ddd` |
| `alp1324-spy-0731` | SPY | `4d36474c49d844c6b1fc388e794292e4` | `690d1308ee8a440da60300dcdd7369f7` |

The roster showed all eight post-restart instances as `Working`, with no
exposure and zero fills before the opening bell.

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
state as `RUNNING`, while Resume admits only `STOPPED`. `68373ea3` adds both
the correct transition and a boot-time repair for already-stranded artifacts.
The production fleet then resumed entirely through panel controls.

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
before the opening bell, so the end preceded the start. `afde86ba` returns a
truthful empty session chart pre-open. The regression suite passed, and the
signed-in panel subsequently rendered the LIVE chart while the service logged
HTTP 200 responses.

### Interrupted bots had no legal panel recovery action

The boot sweep left desired state `RUNNING` after marking the task interrupted.
The panel correctly requires `STOPPED` before Resume, so Start was disabled.
`68373ea3` makes restart recovery a durable fail-closed STOPPED transition and
repairs artifacts produced by the earlier behavior. Focused bot-runner,
boot-recovery, and panel tests passed; the production panel then resumed all
eight bots.

## Validation receipts

- Backend Alpaca/panel/runner/engine scope: 585 passed; one unrelated local
  credentials test is overridden by the developer `.env`.
- Final chart and panel regression scope: 104 passed.
- Final boot-recovery, bot-runner, and panel scope: 142 passed.
- Angular: 237 files, 1,822 tests passed.
- Focused panel Angular: 15 files, 76 tests passed.
- Ruff: application and test scopes passed.
- OpenAPI snapshot check passed.
- Angular generated-type check passed.
- TypeScript `--noEmit` passed.
- Scoped panel ESLint passed with zero warnings.
- Repo-wide strict zero-warning ESLint reports 148 pre-existing warnings
  outside this change; the CI command permits warnings.

The broad Python suite is not represented as green from this workstation.
Its CI-shaped run first encountered an incomplete local SPY LEAN cache, then
a control-intent authorization expectation changed by the active local
data-plane environment. Changed scopes and their transitive Alpaca/panel
coverage are green.

## Market-hours order and fill ledger

Pending the regular-session strategy window.

| SID | Submitted orders | Filled orders | Buy fills | Sell fills | Terminal exposure |
| --- | ---: | ---: | ---: | ---: | ---: |
| `alp1324-aapl-0731` | pending | pending | pending | pending | pending |
| `alp1324-amd-0731` | pending | pending | pending | pending | pending |
| `alp1324-amzn-0731` | pending | pending | pending | pending | pending |
| `alp1324-meta-0731` | pending | pending | pending | pending | pending |
| `alp1324-msft-0731` | pending | pending | pending | pending | pending |
| `alp1324-nvda-0731` | pending | pending | pending | pending | pending |
| `alp1324-qqq-0731` | pending | pending | pending | pending | pending |
| `alp1324-spy-0731` | pending | pending | pending | pending | pending |

## Terminal wind-down

Pending. The required path is:

1. use `Flatten & stop` for any bot with attributed exposure;
2. use `Stop` for bots proven flat;
3. use no direct bot-runner route;
4. verify zero running bots, zero positions, zero open orders, zero uncertain
   intents, no hold or freeze, and fresh clean reconciliation.

## Deviations

- No direct runner-route mutation has been used.
- Two live defects were fixed and regression-tested before the market-hours
  exercise; neither is being hidden as a test-only finding.
- The broad local Python suite has environment-specific blockers documented
  above. This audit does not relabel those runs as green.

## Verdict

**Pending.** Premarket acceptance is proven. Final acceptance requires the
market-hours order/fill exercise and Clerk-led terminal wind-down.
