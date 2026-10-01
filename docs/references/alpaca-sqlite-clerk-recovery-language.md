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

The actions, their availability proofs and their confirmation copy are the
descriptor catalog in `app/broker/alpaca/clerk/sqlite/recovery_policy.py`.

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
account-wide reconciliation leaves the freshness window. Operation-history
pagination never limits the working-order or uncertainty evidence used by policy.
