# Bot Cockpit Trader-Language Design

Date: 2026-06-22  
Status: Proposed  
Scope: `/broker/instances/:id` and all cockpit-v2 trader-visible copy  
Audience: traders who understand positions, orders, sessions, and paper trading but do not operate the application infrastructure

## Deployment-model decision

Traders using this cockpit **are the deployment operators for their bot instances**. They may need to run a safe, copyable command on the machine hosting the application. The cockpit must guide that recovery without requiring them to understand the host-daemon architecture.

This resolves the previous contradiction between trader-friendly operation and the non-goal of “teaching traders to operate the host daemon.” The actual non-goal is:

> Do not require traders to diagnose or understand infrastructure internals. When host action is required, provide one server-authored command, explain its trading consequence, and keep implementation detail in Technical details.

Commands are state-specific:

- **Host service unavailable:** `./start-live-daemon.sh --background` is the repository-supported recovery command.
- **Bot process `EXITED` or `IDLE` while the host service is reachable:** starting the daemon again is not sufficient. The remedy must start this bot's process through a server-authored per-instance action or command.
- **Bot permanently `STOPPED`:** neither command is the recovery path; the trader must redeploy.

The cockpit must not tell a trader to run `./start-live-daemon.sh` for an `EXITED` child process unless the host service itself is also unavailable. That script starts the host daemon; it does not, by itself, restart the exited strategy subprocess.

### Architectural permission for Start bot process

No new ADR is required to expose **Start bot process**:

- ADR 0006 explicitly defines stage 2 as host daemon `POST /runs/{run_id}/start` and says the console Start/Stop port lands that operation in the UI.
- ADR 0006 §1 describes the existing forwarding path as `LiveRunsService.startHostRunner` → data plane → daemon.
- ADR 0007 says the live-instances UI includes the deploy/Start/Stop flow and secures every daemon capability with a mandatory shared-secret token.
- `POST /api/live-instances/runs/{run_id}/start` already implements the browser-safe data-plane proxy; the browser never receives the daemon token.

The cockpit-v2 `host-process-notice.component.ts` comment claiming ADR 0003/0007 prohibit Start/Stop is stale. ADR 0003 makes the host daemon the lifecycle authority; it does not prohibit the authenticated console from requesting lifecycle actions through that authority. Phase 0 must correct that comment and restore the already-authorized Start affordance in cockpit-v2.

The durable **Stop instance** action and **Stop bot process** are different operations. The trader cockpit should expose Start bot process when needed, but it must not reintroduce a casual process-stop control that could be confused with durable retirement.

Process start is not added to `operator_surface.actions`; ADR 0010 intentionally fixes that block to five trading-control actions. It belongs on the host lifecycle projection:

```ts
host_process: {
  state: HostProcessState
  notice: string | null
  copyable_command: string | null
  start_capability: {
    enabled: boolean
    run_id: string | null
    request: HostRunnerStartRequest | null
    disabled_reason_code: string | null
  }
}
```

Python builds `request` from the persisted `start_defaults` / run ledger. Angular must not copy the legacy paper-run page's hard-coded strategy, order cap, or IBKR host defaults. The start button invokes the existing proxy with this server-authored request.

## Outcome

The cockpit should answer three questions before it exposes implementation detail:

1. **What is the bot doing now?**
2. **Is my account at risk?**
3. **What should I do next?**

The current cockpit accurately exposes independent system facts, but it gives each fact equal visual weight and frequently explains blocked actions with infrastructure terms such as `desired-state`, `live binding`, `sidecar`, `host runner`, `WAL`, and `control plane`. Those terms are useful for engineering diagnosis, not for the primary trader workflow.

This proposal keeps the existing server-authored verdicts and fail-closed action gates. It changes their presentation into:

- a primary trader summary;
- an explicit account-risk statement;
- one recommended next action;
- concise reasons attached to unavailable actions;
- technical evidence behind an **Advanced details** disclosure.

This is a presentation redesign, not a simplification of the safety model.

## Design principles

### 1. Lead with the trading consequence

Prefer:

> This bot is stopped and cannot place orders.

Over:

> PROCESS · IDLE / INTENT · STOPPED / READINESS · BLOCKED

The independent facts remain visible, but they support the conclusion instead of competing with it.

### 2. Say what is known, what is unknown, and why it matters

`UNKNOWN` is not trader-facing copy. Every unknown state must identify the subject and consequence:

- **Broker connection not confirmed** — the cockpit cannot verify that IBKR is connected.
- **Paper account not confirmed** — starting is blocked until the account is verified as paper.
- **Previous run status unavailable** — no reliable result was recorded for the last run.

Never render a bare `UNKNOWN` without a noun.

### 3. Give one next step

When several guards block the same action, do not show several paragraphs at equal prominence. Show:

- one primary blocker selected by the existing server priority;
- one recommended action;
- a count and disclosure for additional checks.

Example:

> **Redeploy this bot to run it again.**  
> This bot was stopped permanently. Two additional safety checks will run after redeployment.

The primary message is a single backend-authored remediation object. It owns the headline, explanation, and affordance together. A separate free-form host notice must not compete with it.

### 4. Use trader vocabulary in the primary layer

Primary vocabulary:

- bot
- strategy
- account
- broker / IBKR
- paper account
- position
- open order
- order placement
- trading session
- start, pause, close positions, stop, redeploy
- check, warning, unavailable, needs attention

Advanced-only vocabulary:

- process
- durable intent
- live binding
- host runner / daemon
- sidecar
- control plane
- WAL
- reconciliation receipt
- mutation
- run identity
- poison sentinel

### 5. Describe effects, not implementation primitives

Prefer:

> Pause after the current check. The bot stays available but will not open new positions.

Over:

> Write durable desired state PAUSED.

Prefer:

> Redeploy creates a fresh bot run using this configuration.

Over:

> Redeploy creates a new run identity.

### 6. Do not imply that disabled controls are available actions

If an action is structurally irrelevant, replace the disabled button with the appropriate action.

For a `STOPPED` bot:

- do not present **Resume** as the primary action;
- present **Redeploy** as the primary action;
- explain that the stopped run is retained for history and cannot be restarted.

The disabled Resume capability may remain in Advanced details for contract inspection.

### 7. Preserve independent facts without inventing a master verdict

ADR 0013 explicitly says:

> `operator_surface` contains verdicts, semantic classifications, capabilities, attention-routing inputs, notices, and remediation descriptors.

That passage permits backend-authored notices and remediation descriptors. It does **not** clearly permit a new composite `state_code` presented as “the bot status”; the same ADR rejects a synthetic master status and requires tests to assert the independent PROCESS, INTENT, READINESS, BROKER, and SAFETY facts.

Therefore this design requires a **Phase 0 amendment to ADR 0013** before adding `trader_guidance`. The amendment must permit a ranked narrative summary only under these constraints:

- it is presentation, not a new safety verdict;
- it never enables or disables an action;
- it never replaces or mutates the independent facts;
- its recommended action references an existing server-authored capability or remediation descriptor;
- the independent facts remain rendered and independently asserted in every scenario test;
- its name is `trader_guidance`, not `state` or `master_status`.

Any summary that affects:

- action availability;
- attention routing;
- automatic expansion;
- recommended remediation;

must be authored or selected by Python. Angular may map closed codes to display labels and explanatory copy.

## Guidance priority

The backend selects exactly one primary guidance item using this priority order:

1. **Immediate account exposure:** confirmed owned positions or open orders requiring trader action. Recommend **Close positions and pause** only when live actuation is available and paper safety is confirmed. If no bot process is bound or safety is not `PAPER_ONLY`, use the broker-direct risk-reduction workflow defined below.
2. **Possible live-account danger:** safety `UNSAFE`. Recommend pausing new entries if the bot can act, then verifying the IBKR account.
3. **Safety halt from the previous run:** prior run `HALT_TRIGGERED`. Recommend reviewing the halt evidence and reconciling the account before redeploying. This is distinct from an ordinary exited process.
4. **Unexpected process error:** prior run `EXITED_WITH_ERROR`. Recommend reviewing Warnings and interruptions before starting the same run again.
5. **Permanently stopped or retired run:** `STOPPED_REQUIRES_REDEPLOY`, `REDEPLOY_REQUIRED`, or poisoned. Recommend **Redeploy bot** after any required halt/error review.
6. **Uncertain prior control or order outcome:** unresolved mutation or uncertain order intent. Recommend reconciliation.
7. **Broker/account reconciliation mismatch:** failed, stale, or unreadable reconciliation evidence. Recommend reconciliation.
8. **Current evidence unavailable:** stale runtime or unreachable control channel. Recommend restoring status before starting or closing positions.
9. **Host lifecycle action required:** host service unavailable, bot process exited, or start waiting on the host. Recommend the one state-specific host action.
10. **Other required trading check:** readiness `BLOCKED`, followed by `DEGRADED`.
11. **Paused by trader:** recommend **Start trading** only when Resume is enabled.
12. **Ready but outside the permitted session:** no action; state when trading can next occur.
13. **Ready and active:** no remediation action.

Within a priority, use the existing `sort_reason_codes()` order. In the example `EXITED + broker DISCONNECTED + safety UNKNOWN`, safety `UNKNOWN` is a required verification issue but not evidence of live-account danger. Current evidence restoration ranks before host lifecycle only when status is too stale to trust the process state; otherwise the process-start remedy is primary and broker verification appears as one additional attention group.

For `STOPPED + broker DISCONNECTED + safety UNKNOWN`, **Redeploy bot** is primary because the current run cannot resume under any broker state. Broker verification is shown as one additional requirement that must pass before the fresh run can trade.

For `HALT_TRIGGERED + STOPPED`, the primary situation is **Safety halt requires review**, not generic permanently stopped. `primary_remediation.kind` is `focus_view` targeting Warnings and interruptions. Redeploy remains the eventual action, but only after the halt evidence and broker account have been reviewed.

For `EXITED_WITH_ERROR + process EXITED`, the primary situation is **Bot stopped unexpectedly**, not generic bot process not running. `primary_remediation.kind` is `focus_view` targeting Warnings and interruptions; **Start bot process** remains available as a secondary host-lifecycle control when `host_process.start_capability.enabled` is true.

Guidance ranking does not disable Start. Any actual Start prohibition must come from `host_process.start_capability`, and the start endpoint must re-evaluate the same capability before launch. A review recommendation alone is not a server gate.

### Exposure with no live binding

Starting a failed bot solely to gain access to `Flatten and pause` is unsafe: the child may resume strategy evaluation before the flatten command is processed. When positions or open orders are confirmed but no live binding exists, the primary remediation is a guided broker-direct workflow:

1. write durable `PAUSED` when Pause is available;
2. open IBKR TWS / Client Portal;
3. cancel the bot's open orders;
4. close or otherwise manage the confirmed positions directly at the broker;
5. refresh the cockpit and reconcile before starting or redeploying.

The guidance kind is `broker_manual_risk_reduction`. It does not pretend the cockpit can close positions without a live bot process. The primary label is:

> Secure account in IBKR

The cockpit must identify the bot-owned symbols and quantities from server-authored evidence. This is research tooling, not financial advice; the trader remains responsible for the broker-side order decisions.

## Proposed page hierarchy

### 1. Bot summary

The sticky header becomes a sentence-first status surface.

```text
dep_val_smoke_002                                      PAPER ACCOUNT
Stopped — cannot place orders
No positions or open orders confirmed

[ Redeploy bot ]                                      [ More actions ⋯ ]
```

When attention is required:

```text
dep_val_smoke_002                                      SAFETY CHECK
Not ready to trade
Paper account and order-placement settings have not been confirmed.

[ Review required checks ]                            [ Pause bot ]
```

The compact technical indicators move into **Advanced details**:

```text
Advanced details
Bot process: Idle
Requested mode: Stopped
Trading checks: Blocked
IBKR connection: Not confirmed
Account safety: Paper only
Previous run: Not available
Market session: Closed
```

This preserves the independent assertions required by ADR 0013 and the Playwright meta-rule.

### 2. Account risk

This must always be visible near the summary:

```text
ACCOUNT RISK
No positions or open orders confirmed
```

Or:

```text
ACCOUNT RISK · ACTION REQUIRED
Long 40 SPY · 2 open orders · +$148.20 unrealized
[ Close positions and pause ]
```

Use **Not confirmed** when broker evidence is unavailable. Do not render `FLAT` when flatness has not been proven.

### 3. Required action

Show only when the trader needs to act:

```text
WHAT TO DO NEXT
Redeploy this bot to run it again.
This stopped run cannot be restarted. Redeploy uses the saved configuration
and creates a fresh run.

[ Redeploy bot ]
```

If multiple blockers apply:

```text
WHAT TO DO NEXT
Confirm the connected IBKR account is a paper account.
Starting is blocked to prevent orders from reaching an unverified account.

2 more checks also need attention
```

### 4. Status & Risk

Rename and reorganize:

- **Can it trade?** → **Trading availability**
- **Current risk** → **Account risk**
- **Passing gates** → **Checks passed**
- `hard` → **Required**
- `soft` → **Advisory**
- `fail` → **Needs action**
- `unknown` → **Not confirmed**
- `No inline fix` → **No action is available here**

Each failed check uses four fields:

```text
Paper account verification                         NEEDS ACTION
We cannot confirm that the connected IBKR account is a paper account.
Why this matters: starting is blocked to prevent live-account orders.
[ Check broker connection ]
```

The raw gate name and raw detail move to Advanced details.

### 5. Activity

Trader-facing labels:

- **Latest signal** → **Latest strategy decision**
- **Trade chart** → **Price and trades**
- **Working / Pending orders** → **Open orders**
- **Broker activity** → **Broker executions**
- **Incidents** → **Warnings and interruptions**

Keep broker-authored execution narratives from ADR 0014. They already follow the correct pattern: event, explanation, consequence.

### 6. Configuration

The default view should describe the trading rules, not serialization fields:

```text
STRATEGY
SPY EMA crossover

ORDER SETTINGS
Paper account · Up to 5 orders per day · Market orders

POSITION SIZING
1% target allocation per entry

[ Redeploy with these settings ]
```

Move these fields under **Technical configuration**:

- `strategy_key`
- `spec_path`
- `schema_version`
- `readonly`
- `hydrate_policy`
- `instrument_surface`
- raw sizing policy JSON
- action-plan JSON
- lineage IDs and millisecond timestamps

Configuration reason codes need a trader-copy catalog. Raw values such as `STRATEGY_KEY_MISSING` must never appear in the primary UI.

### 7. Audit

Rename the tab **Run details**. It is useful, but it is not a primary trader workflow.

Sections:

- **Deployment details**
- **Strategy version**
- **Reference backtest**
- **Technical configuration**
- **Emergency retirement**

Rename destructive copy:

- **Mark this run POISONED** → **Retire this run permanently**
- **Mark POISONED (type HALT)** → **Permanently retire run…**

Confirmation:

> Permanently retire this run?  
> It will never be allowed to trade again. Existing broker positions are not automatically closed. Reconcile the account first, then type `HALT` to continue.

The underlying `POISONED` classification remains visible in Advanced details and audit logs.

## State-label catalog

| Technical value | Primary trader label | Supporting copy |
|---|---|---|
| Process `RUNNING` | Bot is online | The bot process is running. Trading still depends on the checks below. |
| Process `IDLE` | Bot is not running | The host service is available, but this bot has no active process. Start this bot's process. |
| Process `WAITING_FOR_HOST` | Start is waiting | Trading was requested, but this bot's process has not started. Start this bot's process. |
| Process `STOPPING` | Stopping | The bot is shutting down. |
| Process `EXITED` | Bot is not running | The previous bot process ended. Review the exit reason, then start this bot's process. |
| Process `UNREACHABLE` | Bot service is offline | Start the bot service on the host machine with the provided command. |
| Intent `RUNNING` | Trading requested | The bot should trade when all safety checks pass. |
| Intent `PAUSED` | Paused | The bot must not open new positions. |
| Intent `STOPPED` | Permanently stopped | This run cannot be resumed. Redeploy to trade again. |
| Readiness `READY` | Ready to trade | All required trading checks pass. |
| Readiness `BLOCKED` | Trading blocked | At least one required check is failing. |
| Readiness `DEGRADED` | Needs review | Trading may be unavailable or operating with incomplete evidence. |
| Readiness `UNKNOWN` | Trading availability not confirmed | The cockpit does not have enough current evidence. |
| Broker `CONNECTED` | IBKR connected | The broker session is available. |
| Broker `DISCONNECTED` | IBKR disconnected | No new broker activity can be confirmed until the connection returns. |
| Broker `UNKNOWN` | IBKR connection not confirmed | The cockpit cannot verify the broker session. |
| Safety `PAPER_ONLY` | Paper account confirmed | The account is confirmed safe for paper trading. |
| Safety `UNSAFE` | Possible live account | Trading is blocked because non-paper account signals were detected. |
| Safety `UNKNOWN` | Paper account not confirmed | Trading is blocked until paper-only status is verified. |
| Prior run `CLEAN` | Previous run ended normally | No halt or process error was recorded. |
| Prior run `HALT_TRIGGERED` | Previous run halted for safety | Review the incident before redeploying. |
| Prior run `EXITED_WITH_ERROR` | Previous run stopped unexpectedly | Review warnings and interruptions. |
| Prior run `UNKNOWN` | Previous run status unavailable | No reliable result was recorded. |
| Session `RTH` | Regular market session | Strategy activity is permitted by the configured session policy. |
| Session `PRE` | Pre-market | Whether the strategy may act depends on its configured session. |
| Session `POST` | After-hours | Whether the strategy may act depends on its configured session. |
| Session `CLOSED` | Market session closed | The strategy is not permitted to act now. |
| Session `UNKNOWN` | Market session not confirmed | Session timing information is unavailable. |

The trader does not need six equal-weight process labels. `IDLE`, `WAITING_FOR_HOST`, and `EXITED` share the primary consequence **Bot is not running**. Their distinction appears in the explanation because it changes the next step or the evidence to review:

- `IDLE`: no process is attached;
- `WAITING_FOR_HOST`: start was already requested;
- `EXITED`: a prior process ended and its exit reason should be reviewed.

`UNREACHABLE` remains distinct because the remedy is different: restore the host service before any per-bot process action is possible.

## Action-language catalog

| Current action | Proposed label | Always-visible explanation |
|---|---|---|
| Resume | Start trading | Allow the bot to open new positions when its strategy signals and all checks pass. |
| Pause | Pause new entries | Keep the bot online, but prevent it from opening new positions. |
| Flatten and pause | Close positions and pause | Cancel this bot's open orders, close its owned positions, and prevent new entries. |
| Stop instance | Permanently stop this run | End this run. It cannot be resumed; trading again requires redeployment. |
| Redeploy | Redeploy bot | Create a fresh run using the selected configuration. This does not itself start the external bot process. |
| Mark poisoned | Permanently retire run | Mark this run unsafe so it can never start again. |

“Start trading” is only a display label for the guarded Resume capability. It does not change the underlying action contract.

## Disabled-action copy pattern

Every unavailable action should render:

```text
<Plain consequence>. <Next step>.
```

Do not include implementation terms, endpoint paths, or runbook filenames in the primary message.

Examples:

| Reason code | Trader-facing copy |
|---|---|
| `STOPPED_REQUIRES_REDEPLOY` | This run was permanently stopped. Redeploy the bot to trade again. |
| `NO_LIVE_BINDING` | This bot is not currently running, so there is nothing to control. Start the bot process from the deployment environment. |
| `BROKER_SAFETY_UNSAFE` | A paper account could not be confirmed and live-account signals were detected. Verify the IBKR account before starting. |
| `BROKER_SAFETY_UNKNOWN` | The connected account has not been confirmed as paper. Restore the IBKR connection and verify the account. |
| `SUBMISSION_CAPABILITY_BLOCKED` | This deployment is not configured to place the expected paper orders. Review the order-placement settings and redeploy. |
| `SUBMISSION_CAPABILITY_UNKNOWN` | The bot's order-placement setting could not be verified. Wait for bot status to refresh or redeploy if it remains unavailable. |
| `NO_OWNED_POSITIONS` | This bot has no confirmed positions to close. |
| `POSTURE_DEMOTED` | Current bot or broker information is stale. Wait for live status to return before starting or closing positions. |
| `UNRESOLVED_UNCERTAIN_INTENT` | A previous order may still be unresolved. Reconcile the bot with IBKR before starting. |
| `UNCERTAIN_INTENT_STATE_UNKNOWN` | The bot's recent order state cannot be verified. Reconcile with IBKR before starting. |
| `RECONCILIATION_FAILED` | The bot's records do not match IBKR. Resolve the differences before starting. |
| `RECONCILIATION_STALE` | The last account check is out of date. Run reconciliation again before starting. |
| `RECONCILIATION_UNKNOWN` | The last account check could not be read. Reconcile again before starting. |
| `MUTATION_UNRESOLVED_*` | The previous request may or may not have completed. Check the bot's current state before trying again. |
| `OUTCOME_UNKNOWN` | The app did not receive a reliable result for the previous request. Check the bot's current state before retrying. |
| `ALREADY_RUNNING` | The bot is already allowed to trade. |
| `ALREADY_PAUSED` | New entries are already paused. |
| `REDEPLOY_REQUIRED` | This run has been permanently retired. Redeploy the bot to trade again. |

The engineering procedure, reason code, endpoint, and runbook link belong in an expandable **Technical details** block.

## The stopped-state example

### Current

```text
PROCESS · IDLE
INTENT · STOPPED
READINESS · BLOCKED
BROKER · UNKNOWN
SAFETY · PAPER_ONLY
LAST RUN · UNKNOWN
SESSION · CLOSED

Resume
Bot is STOPPED. Resume from STOPPED is a Redeploy, not a desired-state write.

Flatten and pause
No live binding — the host runner is not bound to this instance.
```

### Proposed

```text
dep_val_smoke_002                                      PAPER ACCOUNT
Permanently stopped
This run cannot place orders or be restarted.

ACCOUNT RISK
Positions and open orders are not confirmed because IBKR is not connected.

WHAT TO DO NEXT
Redeploy this bot to trade again. The saved configuration can be reviewed
before creating the new run.

[ Redeploy bot ]                                      [ View run details ]

Additional checks after redeployment
• Restore the IBKR connection.
• Confirm the bot is configured to place paper orders.

Advanced details
Bot process: Not running
Requested mode: Permanently stopped
Trading checks: Blocked
IBKR connection: Not confirmed
Account safety: Paper account confirmed
Previous run: Status unavailable
Market session: Closed
```

Important correction: `PAPER_ONLY` and broker `UNKNOWN` are not contradictory. The first is an account-safety verdict from durable evidence; the second is current session connectivity. The proposed copy names the subject of each fact so a trader can understand both.

## Copy ownership and contract changes

### Keep server authority

Python remains authoritative for:

- verdicts and classifications;
- action capability and blocker priority;
- suggested remediation action;
- account-risk posture;
- automatic attention routing;
- backend-authored broker execution narratives;
- any free-form notice that relies on evidence not represented by a closed enum.

### Add a trader-guidance projection

After the ADR 0013 amendment, extend `operator_surface` additively with one backend-authored guidance object:

```ts
trader_guidance: {
  situation_code:
    | 'ACCOUNT_EXPOSURE'
    | 'POSSIBLE_LIVE_ACCOUNT'
    | 'SAFETY_HALT_REVIEW'
    | 'PROCESS_ERROR_REVIEW'
    | 'PERMANENTLY_STOPPED'
    | 'PERMANENTLY_RETIRED'
    | 'CONTROL_OUTCOME_UNCERTAIN'
    | 'ORDER_OUTCOME_UNCERTAIN'
    | 'RECONCILIATION_REQUIRED'
    | 'CURRENT_STATUS_UNAVAILABLE'
    | 'HOST_SERVICE_OFFLINE'
    | 'BOT_PROCESS_NOT_RUNNING'
    | 'WAITING_FOR_BOT_PROCESS'
    | 'TRADING_CHECKS_BLOCKED'
    | 'PAUSED'
    | 'READY_OUTSIDE_SESSION'
    | 'READY'
  headline: string
  explanation: string
  risk_headline: string
  risk_explanation: string | null
  primary_remediation:
    | {
        kind: 'invoke_capability'
        capability: 'resume' | 'pause'
        label: string
      }
    | {
        kind: 'focus_action'
        action: 'flatten_and_pause' | 'stop' | 'mark_poisoned'
        label: string
      }
    | {
        kind: 'focus_view'
        tab: 'status' | 'activity' | 'audit' | 'configuration'
        section: string
        label: string
      }
    | {
        kind: 'redeploy'
        label: string
      }
    | {
        kind: 'copy_host_command'
        label: string
        instruction: string
        command: string
      }
    | {
        kind: 'start_bot_process'
        label: string
        instruction: string
      }
    | {
        kind: 'broker_manual_risk_reduction'
        label: string
        instruction: string
        pause_first: boolean
        owned_positions: Record<string, number>
        pending_order_count: number | null
      }
    | {
        kind: 'open_runbook'
        slug: string
        label: string
      }
    | {
        kind: 'none'
      }
  additional_attention_groups: AttentionGroup[]
}
```

Rationale:

- the guidance determines visual priority and the recommended action, so it must not be composed in Angular;
- it avoids Angular recombining process, intent, readiness, safety, and capabilities into an invented master state;
- the backend can test the bounded situation-code decision table deterministically;
- Angular remains a renderer.

`situation_code` is a presentation classification, not a safety verdict. The closed set is exactly the 17 values above. Adding or splitting a value requires:

- an ADR 0013 compatibility review;
- a decision-table row;
- a fixture and backend projection test;
- a trader-visible copy review.

The implementation does not test the full Cartesian product. It table-tests:

- every one of the 17 winning situations;
- every adjacent priority collision;
- the named hazardous collisions in this document;
- preservation of all independent source facts.

Named hazardous collisions are:

1. owned positions + no live binding;
2. owned positions + safety `UNSAFE`;
3. owned positions + stale runtime evidence;
4. `HALT_TRIGGERED` + `STOPPED`;
5. `HALT_TRIGGERED` + owned positions;
6. `EXITED_WITH_ERROR` + process `EXITED`;
7. process `EXITED` + broker `DISCONNECTED` + safety `UNKNOWN`;
8. intent `STOPPED` + broker `DISCONNECTED` + safety `UNKNOWN`;
9. host service `UNREACHABLE` + intent `RUNNING`;
10. `WAITING_FOR_HOST` + readiness `BLOCKED`;
11. unresolved mutation + requested retry of the same action;
12. reconciliation failure + Resume otherwise enabled;
13. market `CLOSED` + readiness `READY`;
14. poisoned + owned positions;
15. broker connection `UNKNOWN` + safety `PAPER_ONLY`.

Expected winners:

| Collision | Situation code | Primary remediation |
|---|---|---|
| owned positions + no live binding | `ACCOUNT_EXPOSURE` | `broker_manual_risk_reduction` |
| owned positions + safety `UNSAFE` | `ACCOUNT_EXPOSURE` | `broker_manual_risk_reduction` with `pause_first=true`; do not automate liquidation against a possibly live account |
| owned positions + stale runtime evidence | `ACCOUNT_EXPOSURE` | `broker_manual_risk_reduction`, clearly labeled last-known |
| `HALT_TRIGGERED` + `STOPPED` | `SAFETY_HALT_REVIEW` | `focus_view` |
| `HALT_TRIGGERED` + owned positions | `ACCOUNT_EXPOSURE` | risk reduction; halt review is additional |
| `EXITED_WITH_ERROR` + process `EXITED` | `PROCESS_ERROR_REVIEW` | `focus_view` |
| process `EXITED` + broker `DISCONNECTED` + safety `UNKNOWN` | `BOT_PROCESS_NOT_RUNNING` | `start_bot_process`; account verification is additional |
| intent `STOPPED` + broker `DISCONNECTED` + safety `UNKNOWN` | `PERMANENTLY_STOPPED` | `redeploy`; account verification is additional |
| host service `UNREACHABLE` + intent `RUNNING` | `HOST_SERVICE_OFFLINE` | configured `copy_host_command` or `open_runbook` |
| `WAITING_FOR_HOST` + readiness `BLOCKED` | `WAITING_FOR_BOT_PROCESS` | `start_bot_process`; trading checks are additional |
| unresolved mutation + retry of same action | `CONTROL_OUTCOME_UNCERTAIN` | reconciliation/runbook flow |
| reconciliation failure + Resume otherwise enabled | `RECONCILIATION_REQUIRED` | reconciliation flow |
| market `CLOSED` + readiness `READY` | `READY_OUTSIDE_SESSION` | `none` |
| poisoned + owned positions | `ACCOUNT_EXPOSURE` | `broker_manual_risk_reduction`; retirement is additional |
| broker `UNKNOWN` + safety `PAPER_ONLY` | winner from process/intent/readiness | account verification is additional; paper safety is not overwritten |

A future collision joins this list when either condition holds:

- choosing the wrong primary remediation could increase account exposure or enable trading incorrectly; or
- two inputs imply materially different trader actions and can occur simultaneously.

Every named collision gets a fixture asserting `situation_code`, `primary_remediation.kind`, additional attention groups, and the unchanged independent facts.

`primary_remediation` replaces the previous combination of free-form notice plus recommended button. It is the one prominent next step. Supporting notices may appear only inside the same remediation card or under Technical details.

`copy_host_command.command` is environment-bound, authored by the backend, and rendered verbatim. It comes from deployment configuration, not from a frontend constant and not from an unconditional projection hardcode:

```text
LIVE_RUNNER_HOST_START_COMMAND
```

Rules:

- local macOS/Linux repo deployment sets `./start-live-daemon.sh --background`;
- Windows service deployment sets the approved NSSM/service-start command;
- externally supervised or remote-host deployment leaves the value unset and supplies an `open_runbook` remediation instead;
- the backend emits `copy_host_command` only when the configured command is non-empty and the control-plane state proves the host service is unavailable;
- command provenance is included in Technical details as `configured`, never guessed.

For the current macOS/Linux development deployment, the configured value is:

```text
./start-live-daemon.sh --background
```

The schema comment for `command` must state that it is an environment-specific operator instruction sourced from trusted server configuration. A future deployment-mode enum may replace the free-form setting when the repository supports more than one production topology; until then, unset means “no safe command can be authored.”

The backend must not emit that command for `BOT_PROCESS_NOT_RUNNING`; that situation uses the already-authorized per-instance Start endpoint. Failure to build a valid start request is an implementation error surfaced as:

> This bot cannot be started from the cockpit because its saved start settings are incomplete. Review Configuration and redeploy.

There is no permanent **Contact the deployment operator** path for this local single-operator deployment model.

`READY` exists to author the calm headline and explanation when no remediation is required. Exposure cannot produce `READY`: `ACCOUNT_EXPOSURE` outranks it. The former `READY_FLAT` / `READY_WITH_EXPOSURE` split was redundant and is removed.

### Attention-group counting rule

`additional_attention_count` is replaced by `additional_attention_groups`, a closed, deduplicated list:

```ts
type AttentionGroup =
  | 'ACCOUNT_EXPOSURE'
  | 'ACCOUNT_VERIFICATION'
  | 'ORDER_PLACEMENT_CONFIGURATION'
  | 'RECONCILIATION'
  | 'CURRENT_STATUS'
  | 'HOST_LIFECYCLE'
  | 'TRADING_CONFIGURATION'
  | 'TRADING_CHECKS';
```

Counting rules:

- exclude the group represented by `situation_code`;
- count each group at most once regardless of how many raw reason codes contributed;
- include only actionable groups;
- do not count market closure, an already-satisfied action, or informational `RECONCILIATION_NOT_AVAILABLE`;
- collapse broker `DISCONNECTED` plus safety `UNKNOWN` into one `ACCOUNT_VERIFICATION` group when both resolve through restoring and verifying the IBKR session;
- collapse all unresolved mutation codes into one `CURRENT_STATUS` group;
- collapse failed/stale/unknown reconciliation codes into one `RECONCILIATION` group.

The UI renders the group labels, not merely an integer:

> 2 more items need attention: Account verification, Trading configuration

### Add closed copy catalogs

Use separate exhaustive catalogs for:

- disabled action reasons;
- configuration reason codes;
- readiness gate labels;
- runtime freshness reason codes;
- account identity reason codes;
- broker-observation consistency reason codes;
- host-process states;
- prior-run classifications.

Each catalog entry should support:

```ts
{
  label: string
  explanation: string
  consequence?: string
  next_step?: string
}
```

Unknown codes must render:

> A new system condition needs attention. View technical details and share code `<CODE>` with support.

Do not show `Unrecognized reason code: ...` as the primary message.

## Progressive disclosure

Three levels:

1. **Trader summary** — consequence, risk, next action.
2. **Checks and explanation** — named checks, why they matter, remediation.
3. **Technical details** — raw enums, reason codes, paths, hashes, process IDs, endpoint/runbook procedure, JSON.

The primary workflow must be usable without opening level 3.

The DOM marks the boundary explicitly:

```html
<section data-copy-layer="primary">...</section>
<section data-copy-layer="explanation">...</section>
<details data-copy-layer="technical">...</details>
```

Copy-regression tests scan only `[data-copy-layer="primary"]` and `[data-copy-layer="explanation"]` for prohibited infrastructure terms. Technical details may contain exact enums, reason codes, commands, paths, and identifiers. Every trader-visible cockpit section must declare one of these markers; an unmarked section fails the structural test.

## Accessibility and interaction requirements

- Disabled controls must not be the only place where the reason is available.
- The recommended next action must be keyboard reachable and have a visible explanation.
- Status cannot rely on color; every state includes text.
- Avoid all-caps paragraphs. Reserve all caps for short category labels.
- Tooltips may supplement visible copy but must not contain unique instructions.
- Destructive actions state whether positions are or are not closed.
- Confirmation dialogs use the trader consequence as the heading.

## Acceptance scenarios

The design is complete only when these scenarios read naturally without technical knowledge:

1. Running, ready, connected paper bot with no positions.
2. Running bot holding a position.
3. Paused bot holding a position.
4. Stopped bot with no process.
5. Start requested while waiting for the external process.
6. Broker disconnected but paper safety still confirmed.
7. Possible live account detected.
8. Submission settings blocked or unavailable.
9. Runtime evidence stale.
10. Previous control request has an unknown outcome.
11. Reconciliation mismatch.
12. Poisoned / permanently retired run.
13. Configuration incomplete.
14. No deployment or no prior run.
15. Market closed while the bot is otherwise ready.
16. Safety-halted prior run.
17. Prior run exited with an error.
18. Confirmed positions with no live bot process.
19. Host service unavailable with a configured recovery command.
20. Host service unavailable without a configured recovery command.
21. Bot process start settings incomplete.

For every scenario, tests must still assert independent PROCESS, INTENT, READINESS, BROKER, and SAFETY values in the Advanced details layer.

## Delivery sequence

### Phase 0 — Contract amendment and tactical recovery

- Amend ADR 0013 to permit `trader_guidance` under the constraints in this document.
- Wire a server-authored `copy_host_command` remedy for host-service-unavailable states.
- Add trusted deployment configuration for the environment-specific host-service start command; use `./start-live-daemon.sh --background` for the current macOS/Linux host deployment.
- Do not use that command for an `EXITED` or `IDLE` bot child while the host service is reachable.
- Restore the already-authorized per-instance **Start bot process** affordance using `LiveRunsService.startHostRunner` → data plane → authenticated daemon.
- Correct the stale cockpit-v2 comment that misstates ADR 0003/0007.

This tactical recovery slice may ship independently because it fixes a concrete missing remedy without changing the page hierarchy.

Phase 0 exit criteria:

- ADR 0013 amendment merged;
- trusted host-start-command configuration documented and tested;
- `UNREACHABLE` renders the configured copyable remedy, or an explicit runbook remediation when no safe command is configured;
- `IDLE`, `WAITING_FOR_HOST`, and ordinary `EXITED` render a working per-instance **Start bot process** action;
- `HALT_TRIGGERED` and `EXITED_WITH_ERROR` make review the primary guidance while Start remains governed independently by `host_process.start_capability`;
- `STOPPED` renders Redeploy, never Start;
- component and API tests pin all paths above.

### Phase 1 — Atomic trader-guidance vertical slice

- Replace raw configuration, freshness, account, and gate codes with exhaustive trader-copy catalogs.
- Rewrite `disabled-reason-copy.ts` using the consequence + next-step pattern.
- Rename tabs, sections, and actions.
- Add `operator_surface.trader_guidance`.
- Implement and table-test the 17-code backend decision table.
- Render the summary, account risk, and recommended action above the independent technical indicators.
- Move the indicators into Advanced details without removing their test IDs or raw `data-value` attributes.
- Add `data-copy-layer` markers and regression tests that reject the primary/explanation-layer terms `desired-state`, `live binding`, `sidecar`, `WAL`, `control plane`, `mutation`, and `run_id`.

The catalog rename and trader-guidance hierarchy are one atomic release. The copy-only half must not ship independently: friendlier labels on the existing flat indicator row do not solve prioritization and would create a misleading “redesign shipped” milestone.

### Phase 2 — Card restructuring

- Convert Trading availability checks to label / explanation / consequence / next-step rows.
- Create the trader-first Configuration summary and Technical configuration disclosure.
- Rename Audit to Run details and rewrite permanent-retirement copy.

### Phase 3 — Scenario validation

- Add fixture coverage for all acceptance scenarios.
- Run moderated usability checks with users who understand trading but do not know this architecture.
- Success criteria:
  - user identifies whether the bot can place orders in under 5 seconds;
  - user identifies current account exposure in under 5 seconds;
  - user selects the correct next action without opening Technical details;
  - user does not interpret `STOPPED` as temporarily paused;
  - user does not interpret `PAPER_ONLY + broker UNKNOWN` as contradictory.

## Non-goals

- Removing safety gates or fail-closed behavior.
- Combining broker connection and paper-account safety.
- Hiding account exposure behind a tab.
- Letting Angular infer action eligibility.
- Requiring traders to understand or diagnose host-daemon internals. Guided, copyable host commands are in scope.
- Turning the cockpit into a general infrastructure dashboard.

## Immediate implementation decision

Take the tactical command path now, with state-correct behavior:

1. For **host service unavailable**, wire `host_process.copyable_command` (or the new guidance equivalent) to `./start-live-daemon.sh --background` and show **Copy start command**.
2. For ordinary **bot process `EXITED` / `IDLE` with a reachable host service**, do not show the daemon command. Restore **Start bot process** through the existing authenticated start endpoint. If saved start settings are incomplete, route to Configuration/Redeploy rather than inventing another operator.
3. For `HALT_TRIGGERED` or `EXITED_WITH_ERROR`, show the review/remediation flow before Start.
4. For confirmed exposure without a live binding, show **Secure account in IBKR** and first persist Pause when available.
5. Keep **Redeploy bot** for permanently `STOPPED` or retired runs.

This is cheap and reversible, but it must not encode the wrong operational remedy merely to place a command on the screen.
