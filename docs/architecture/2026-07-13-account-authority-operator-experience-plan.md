# Account Authority and Seamless Reconciliation — Future Plan

## Decision in one sentence

Every broker-reported paper or live trading account is a durable **Account
Authority**: it owns the broker connection, account ledger, continuous account
observation, trading permission, recovery workflow, and operator remedy
experience for every bot bound to that account.

The broker remains the final source of truth for live facts.  Our ledgers
attribute and explain those facts; they never overwrite or excuse a broker
disagreement.

## Why this exists

Today an operator can be sent to reconcile from several different bot flows.
That makes routine account safety feel like a manual feature, even though a
healthy account is a precondition for most bot activity.  The normal operation
must instead be invisible: configure and verify an account once, keep it
observed continuously, and let a healthy bot start promptly.

Manual reconciliation remains a recovery tool, not a normal prerequisite.

## Ubiquitous language

| Term | Meaning |
|---|---|
| **Broker connection profile** | The configured connection path to a broker session, including the broker-specific Gateway/TWS/API settings needed to connect. It does not claim an account identity merely because text was entered. |
| **Broker-reported account** | The account identifier and operating environment reported by the connected broker after connection. |
| **Account Authority** | The durable account-scoped parent service and audit boundary for one broker-reported paper or live account. |
| **Active Account Binding** | The operator-approved relationship between a broker connection profile and the broker-reported account that this installation is allowed to operate. |
| **Bot Account Binding** | The immutable Account Authority assigned to a bot run before it can be deployed or started. |
| **Account observer** | The passive, continuous process that obtains broker facts, attributes them against durable evidence, and maintains or revokes current account proof. |
| **Account proof** | The short-lived, durable evidence that the account is fresh, identity-matched, observable, and reconciled for ordinary trading. It is not a bot phase. |
| **Account Remedy Center** | The account-scoped operator surface that explains a block and presents the action that can cure it. |

## Account Authority responsibilities

An Account Authority is the parent scope for:

- the selected broker connection and broker-reported account identity;
- paper/live environment verification;
- the account-level bot and order attribution ledger;
- the continuous account observer and its durable observation proof;
- account-wide permission for new orders;
- recovery records, freeze/override records, and account-local audit events;
- the Account Remedy Center.

A bot without a Bot Account Binding is invalid. It cannot be created,
deployed, started, or submit an order. A bot run never silently moves when an
operator changes the selected active account.

## Configuration experience

The existing account values become initial defaults, not permanent static UI
text and not values typed into each bot form.

The Account Configuration page will:

1. Configure the broker connection profile needed for IBKR Gateway/TWS/API
   operation (for example host, port, API/client settings, and operating
   environment).
2. Test the connection and obtain broker-reported account identity and
   paper/live classification.
3. Let the operator select only from accounts returned by the broker when the
   connected session exposes more than one account.
4. Persist an Active Account Binding after explicit operator approval.
5. Refuse an account-ID or paper/live mismatch rather than accepting a display
   label as proof.

The application must publish its own practical IBKR setup documentation and
troubleshooting guide. It should explain every setting needed to make the
Gateway functional in this product, link to the relevant official IBKR pages,
and not require an operator to infer the necessary setup solely from IBKR
documentation.

Changing the active binding is a safety action, not a casual selector change.
Existing bot runs retain their original binding. The change flow must surface
active runs and unresolved exposure on the old authority and require them to be
stopped or explicitly resolved before a new authority is used for new bots.

## Normal operating path

```text
Configure connection profile
  -> connect to IBKR
  -> discover broker-reported account and environment
  -> approve Active Account Binding
  -> observer maintains a fresh account proof
  -> bot start and submit consume that proof
```

The observer is the only routine account reconciler. It reads the connected
account's identity, positions, open orders, and relevant execution/order facts;
compares them with account, bot, and operator evidence; and renews or revokes
the durable proof. It does not place orders, flatten positions, adopt a
crashed bot's exposure, or change bot phase.

For a healthy account, bot start should be prompt:

- If a fresh proof already exists, start uses it without a new navigation or
  separate reconciliation operation.
- If a fresh observation is needed, Start shows a brief **Verifying account**
  progress state and continues immediately after the proof succeeds.
- Start must not wait for an arbitrary observer cadence when a direct fresh
  observation can safely establish the proof.
- The configured trading window and the broker's available capability define
  whether the bot may operate; the operator should not have to fight unrelated
  reconciliation screens during that window.

The existing observation-lease work is the first implementation step toward
this path. It is intentionally in shadow mode until paper-session parity shows
that durable proof is never weaker than the current live submit gate.

## Reconciliation outcomes

Every material broker fact must have one clear outcome:

| Outcome | Meaning | Default effect |
|---|---|---|
| Actively attributable | An active bot and durable ledger evidence explain it. | Ordinary operation continues. |
| Recovery-required | Durable evidence identifies a crashed or retired bot as the owner, but no active manager currently exists. | Pause new trading account-wide pending recovery, resolution, or permitted override. |
| Acknowledged manual activity | An operator durably linked direct broker activity to a broker fact and supplied a resolution policy. | Follow the recorded policy; never silently call it bot-owned. |
| Unattributed activity | No bot evidence, recovery record, or manual acknowledgement explains the broker fact. | Pause new trading account-wide. |
| Unobservable/invalid broker evidence | A required broker fact is unavailable, stale, contradictory, malformed, or from the wrong account. | Pause new trading account-wide. |

Unattributed and unobservable are deliberately different. The first means the
broker returned a fact the system cannot explain. The second means the system
cannot reliably determine the account's present facts. Both are unsafe for new
orders, but need different explanation and remedy.

## Recovery and restart behavior

A normal application restart must not turn durable bot-owned broker exposure
into foreign activity. Durable bot identity, account binding, namespace, order
references, and order/execution evidence preserve attribution.

If a bot crashed while holding exposure:

1. The Account Remedy Center names the exact bot/run and its current broker
   facts.
2. No unrelated bot may silently adopt or manage that exposure.
3. The operator may explicitly revive the identified bot.
4. The revived bot reconciles its ledger, current broker position, and open
   orders before it resumes managing exposure.
5. The successful recovery is written durably and auditable.

The operator may instead resolve the exposure, such as by flattening. Other
bots resume only after a fresh broker observation proves that resolution, or
under an explicit, auditable, time-bounded account override. Requesting a
flatten is not itself proof that the account is reconciled.

## Account Remedy Center

Blocks should not scatter operators across bot-specific reconciliation pages.
Every blocked bot surface routes to the same affected Account Authority's
Remedy Center, which contains:

- the affected broker account and paper/live classification;
- the last verified account state and time, clearly marked stale when needed;
- the exact broker fact or attribution problem that prevented proof;
- the identified bot/run when recovery-required exposure exists;
- a single appropriate action on the same page.

Examples of remedy actions are reconnect/repair Gateway configuration, retry
the observation, select/approve the correct broker-reported account,
investigate or acknowledge manual activity, revive the named bot, resolve or
flatten exposure, and create an audited override where policy permits it.

Healthy state remains quiet: **Account verified**. A failure must be precise,
for example: **Trading paused — open orders unavailable from broker; account
verification cannot complete.**

## Implementation boundaries and rollout

- Implement IBKR first, while keeping the Account Authority vocabulary
  broker-neutral so future adapters do not force a bot/UI redesign.
- Paper and live accounts use the same safety architecture; they never share
  account proof, ledger ownership, or bot bindings.
- Keep flatness-oriented recovery proof separate from ordinary observation
  proof: owned non-zero intraday positions can be safe for normal operation,
  while a freeze-clear or flat-start policy may need stronger recovery proof.
- Keep current submit authority while the durable observation proof is shadowed
  and compared against it.
- Require successful parity evidence from real paper-market sessions before
  making the durable proof the start/submit authority and retiring duplicated
  routine reconciliation paths.

## Prompt for Fable

```text
Act as an independent principal architect reviewing a proposed broker-account
and bot-lifecycle model for a trading application. Be critical and concrete:
identify unsafe assumptions, missing states, race conditions, operator traps,
and ways to simplify the design without weakening broker-account safety.

The broker's account is the final authority for live facts: account identity,
paper/live environment, positions, open orders, fills/executions, and relevant
order status. Application ledgers explain those broker facts; they never
override a disagreement.

Each broker-reported paper or live account is a durable Account Authority. It
is the parent scope for the broker connection, selected account binding,
continuous observer, durable attribution ledger, account-wide order
permission, bot recovery, audit events, and operator remedies. Every bot run
is immutably bound to exactly one Account Authority before it can start or
trade. There can be no unbound bot.

The UI will configure an IBKR connection profile, test it, discover the actual
account(s) and paper/live classification reported by IBKR, and require explicit
operator approval of an Active Account Binding. Account IDs are not typed into
bot forms. The product owns practical IBKR setup documentation and links to
official IBKR documentation.

One passive account observer continuously reads required broker facts and
matches them to bot and operator ledger evidence. It writes a short-lived,
durable account proof only when the connected account is fresh,
identity-matched, observable, and explainable. Bot start and submit consume
that proof. The observer never places trades, flattens positions, adopts
exposure, or changes bot lifecycle phase.

Required classifications are:
- actively attributable: an active bot and durable ledger explain the fact;
- recovery-required: a crashed/retired bot is durably identified as owner but
  no active manager exists;
- acknowledged manual: an operator durably linked direct broker activity to a
  broker fact and a resolution policy;
- unattributed: no bot, recovery record, or manual acknowledgement explains it;
- unobservable/invalid: a required broker fact is unavailable, stale,
  contradictory, malformed, or belongs to the wrong account.

Unattributed and unobservable facts both pause new orders account-wide by
default, but have different operator explanations. An unavailable broker fact
must name the missing fact and show the last verified state; it must not look
like unknown trading activity.

Restarts must preserve attribution. If a crashed bot held exposure, the system
names the exact bot/run and asks the operator to choose recovery. No unrelated
bot may adopt it. On explicit operator request, the exact bot may revive,
reconcile its ledger against current broker positions and open orders, then
resume management. Alternatively, the operator may resolve/flatten exposure.
Other bots resume only after fresh broker proof confirms resolution, or under
an explicit, audited, time-bounded override.

The desired UX is seamless in normal operation: configure an account once;
while its proof is fresh, a bot starts promptly without a manual reconciliation
screen. When blocked, every bot view routes to one Account Remedy Center for
the affected account. It states the exact cause and presents the appropriate
action in place.

Please answer:
1. What is unsafe, ambiguous, over-engineered, or missing?
2. What durable evidence is minimally sufficient to prove ownership after a
   crash, including partial fills and multiple orders contributing to one net
   position?
3. What races exist between observation, submit, fill, gateway disconnect,
   restart, account switching, and recovery?
4. Which conditions should block the whole account versus only one bot?
5. What should be required for a safe, bounded operator override?
6. Propose a simpler state model and an incremental rollout plan, preserving
   the rule that broker truth wins over application memory.
```
