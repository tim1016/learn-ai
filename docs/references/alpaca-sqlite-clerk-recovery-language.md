# Alpaca SQLite Clerk recovery language and action matrix

**Status:** Backend-authored product contract for an activated SQLite account.

The backend owns scope, impact, freshness, availability, confirmation copy, primary
action, and next step. Angular renders these fields and must not infer safety from raw
codes. Opaque order IDs, command IDs, hashes, paths, and evidence references remain
exact; known backend codes shown as receipt evidence pass through the shared
`receiptLabel` pipe.

## Truth language by audience

| Situation | Trader lens | Operator lens | Scope and impact |
|---|---|---|---|
| Healthy and recently reconciled | “Broker and Clerk custody agree.” | Show generation, DB identity/health, control revision, reconciliation age, and exact receipt links. | No new-exposure restriction beyond normal admission policy. |
| Bot-scoped uncertainty | “This bot needs evidence before it can increase exposure.” | Name the bot, uncertain operation/order identities, evidence age, and the exact recovery capability. | `BOT`; other bots are not blocked unless separate account evidence says so. |
| Unknown or unclassified uncertainty | “The account needs operator review. New exposure is paused.” | State that the cause could not be safely classified; link the last readable custody timeline and evidence ages. | `ACCOUNT_CLERK`; fail closed for new exposure. |
| Stream gap or stale broker evidence | “Broker state is being refreshed. New exposure is paused.” | Show the stale clock/channel, last observation, and `Reconcile now` when executable. | Scope authored by policy; risk reduction still requires action-specific fresh proof. |
| SQLite authority failure | “Account control is unavailable. No new broker action is enabled.” | Show the typed startup/health reason and only verified offline recovery capabilities. Never suggest generic retry/clear. | `ACCOUNT_CLERK`; no broker-mutation capability is installed. |
| Reset eligible | “The account is flat and stopped; an operator may create a new control generation.” | Show fresh flat/order-free proof, stopped roster, preservation destination, and generation invalidation warning. | `ACCOUNT_CLERK`; destructive authority recovery, not ordinary hold resolution. |

## Action matrix

| Action ID | Human label | Availability and proof | Effect | Confirmation / retry contract |
|---|---|---|---|---|
| `reconcile_now` | Reconcile now | Healthy authority; broker comparison is callable. | Records a fresh account comparison through SQLite custody. | No generic “retry.” The same typed policy is rechecked immediately before execution. |
| `cancel_verified_working_orders` | Cancel verified working orders | Exact Clerk order reference and Alpaca ID, working state, and fresh broker evidence. | Cancels only the listed identities. | Confirmation lists the proven orders. A stale token has no effect; transport retry returns durable resources. |
| `prepare_safe_flatten` | Prepare safe flatten | Attributed, finite exposure; no working orders or relevant uncertainty; and a fresh successful reconciliation. | Produces a read-only plan with one opposite-side, absolute-quantity leg per nonzero attributed position. The envelope binds the plan to the account, authority generation, database identity, control revision, scope, reconciliation, evidence clocks, expiry, and version token. It submits no broker order. | Preparation rechecks current policy. A changed position quantity or evidence timestamp produces a different token. Any future reduction mutation must recheck the action-specific plan; this capability does not execute it. There is no blind flatten. |
| `stop_bot_decisions` | Stop bot decisions | Bot scope with one active lifecycle run. | Durably stops new decisions while retaining Clerk custody of exposure. | Confirmation warns that exposure is not blindly flattened. Retry returns the existing durable Stop command. |
| `open_custody_timeline` | Open custody timeline | Always available when any readable projection exists. | Navigation only; selects operation-first evidence with broker/source, Clerk-observation, and durable-record clocks. | Never presented as a mutation. |
| `rebuild_from_mirror` | Rebuild from mirror | Typed authority failure plus a contiguous, finalized, account/generation/database-bound, hash-verified mirror. | Offline: preserves DB files and rebuilds the same authority generation. | Only shown for authority failure. Requires explicit confirmation and stopped-process precondition. |
| `reset_authority` | Reset authority | Typed authority failure, every governed bot stopped, fresh matching broker identity, flat finite positions, and no open orders. | Offline: preserves the old generation and creates empty generation `N+1`. | Only shown for authority failure. Confirmation states prior control identities become invalid. |

`clear_hold`, blind `retry`, and unproven `flatten` are absent from activated SQLite
capability. Resolution is an evidence-backed reconciliation, exact cancellation,
versioned reduction plan, stopped decision process, verified rebuild, or verified reset.

## Concurrency and freshness

Each presented capability carries a token over only its action-relevant durable facts:
account, generation, DB identity, optional bot, action, and evidence inputs. Execution
rebuilds the same policy context and compares the token before any effect. Unrelated
chart or bot activity does not stale a token; an action-relevant change does. Evidence
older than the policy window is labeled stale and cannot authorize a mutation that
requires fresh broker truth.

For `prepare_safe_flatten`, the plan version includes each position's attributed
quantity and `updated_at_ms`, along with working-order, uncertainty, and
reconciliation facts. Re-observing a position therefore invalidates the prior
version even when its quantity is unchanged. The plan expires when the successful
reconciliation leaves the freshness window.
