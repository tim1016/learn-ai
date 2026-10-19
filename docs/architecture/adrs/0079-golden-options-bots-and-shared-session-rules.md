# ADR 0079 — Golden Options setups and a shared session-rules implementation

**Status:** Accepted 2026-10-17.
**Provenance:** Owner decisions in the Options bot design conversation on
2026-10-17. The owner requested implementation after exploring the execution,
qualification and recovery seams.

## Decision and why

Deploy offers a Signal bot or an Options bot. A Signal bot follows a Signal
Program and may trade shares or an option structure. An Options bot follows an
exact saved setup's session timing and per-leg premium, stop and re-entry rules.
The distinction is how decisions are made, rather than the asset traded.

Options Backtest and the deployed Options bot use one canonical session-rules
implementation. Historical execution supplies modeled outcomes; deployment
supplies confirmed Clerk outcomes. This supersedes ADR 0078's exclusion of a
connection between saved setups and Deploy. It does not turn Options Backtest
into a backtest of a Signal bot trading option structures.

The existing Clerk remains the account's execution and custody authority.
Each setup is one bot, with durable leg and attempt attribution. A leg-specific
close reduces that attempt's actual contracts; it never resolves a new contract
or relies on a narrower copy of a whole-structure Action Plan. Protection cannot
leave while sold contracts or potentially filling sold orders depend on it.
The broker's four-leg order limit remains distinct from an eight-leg setup.

IBKR remains the market-data provider and Alpaca handles orders and execution
reports. Premium rules read completed one-minute trade bars, as the backtester
does. Bid/ask quotes price and admit orders; quote midpoints do not replace the
tested trade closes. The setup's contract-picking rules are shared as well:
"this week" may select today on the final session of its week.

## Owner decisions

- First-release qualification approves an exact tested saved setup with selected
  completed backtest evidence. Automatic search and walk-forward optimization
  are separate work.
- A golden setup is an immutable approved version, rather than a flag on a
  mutable saved name. Its underlying, trading rules and tested quantities are
  fixed. Edits are a new candidate and inherit no approval.
- The first release supports the existing bought and covered sold leg behaviours,
  including partial and complete square-off.
- A deployed Options bot repeats each trading session until stopped or its
  deployment ends. A new deployment started after entry time waits for the next
  eligible session. Recovery restores an existing session separately.
- **Amended 2026-10-19 (owner):** Options Deploy pre-fills a deployment end
  45 minutes before the canonical calendar's session close, on that session's
  date. Early closes use their actual close. Once the cutoff has passed, the
  default is the next trading session's cutoff. The deployment end closes
  remaining positions and stops repetition; the explicit no-end choice retains
  daily repetition. The default does not rewrite an approved setup's expiry or
  session rules: same-day expiry remains the setup's contract-picking rule.
  A scheduled end must follow the first eligible entry. It is checked before
  funding and again at activation, since the entry can pass during admission.
- Interrupted Options bots require explicit Resume after reconciliation. State
  and re-entry counts survive interruption; a restart cannot earn another entry.
- Stale or revoked qualification blocks entries and re-entries while stops,
  protection and scheduled closing continue.
- A partial entry cancels its remainder and manages the confirmed quantity.
  Stops use its actual average fill price. Re-entry waits for proven opening-order
  termination and confirmed closure, including any late fills.

## Qualification and execution evidence

Approval preserves the exact setup, tested capital and fee/slippage assumptions,
selected run, pinned inputs, replay proof, executable rule identity and the
owner's note. Referenced evidence cannot be deleted. Re-proof and revocation
are recorded without rewriting the original approval. An unverified proof does
not become valid through an owner's performance approval.

An explicit re-proof under new rule bytes can restore qualification for the same
golden version and exact setup. Existing deployment configuration retains its
original rule identity as audit evidence; each resumed run records the current
proof separately. Changed setup parameters require a new approval.

The proof establishes repeatability on the recorded inputs. It does not establish
independent mathematical validation, future performance or live fill equivalence.
The historical replay's atomic opening group is a simulation assumption. Live
execution sequences orders against confirmed cover and records refusals and
partial outcomes explicitly.

Account, mode, budget and order terms are reviewed at deployment under the
existing account gates. Approval starts no bot and enables no live rollout
setting. Existing Signal bot configuration hashes and records remain readable.
The Signal Program canary-pairing ceremony continues to govern Signal Programs.
Options setup qualification uses its exact approval and repeatability proof;
it creates no fictitious Signal Program registration to enter that ceremony.

A scheduled Options budget review reserves cash without pretending to know the
future contracts or their prices. Each triggered entry still requires current
market evidence, confirmed cover and Clerk admission. Explicit Resume retains
the original capital and lifetime results, reserving only its remaining free
balance after a fresh review. Resume without that consent manages exits only.

Options recovery is a limited exception to the retired general Resume control.
Only interrupted duties can resume; operator Stop and a completed deployment
end remain terminal. Interrupted Dry Run allocations survive for reconciliation
and explicit recovery, while intentional Stop retains its existing close policy.

## Alternatives

A second implementation of momentum, stops and re-entry would let a golden
backtest approve rules different from those deployed. A universal event framework
would broaden the interface before there are callers for that flexibility.
Keep a small shared session interface and separate historical/live adapters.
