# ADR 0080 — Options Golden Search owns selection evidence, existing Options authority owns approval

**Status:** Accepted 2026-10-17. **Authority:** owner implementation request for #3332, including its tightened architecture.

## Decision and why

Options Golden Search composes the canonical Options session rules and historical adapter. It does not register a Signal Program or change Signal Golden Search metrics. Python owns its fixed-capital minute drawdown, calendar-annualized daily Sharpe, recovery, gate applicability and deterministic selection. Replay publishes an optional observation after due fills and same-close decisions; observing never changes a trading decision. Missing contemporaneous held-position marks invalidate qualification. Incomplete sessions stop research before ordinary diagnostic marked-value cash carry can fund a fictitious next session.

One protocol freezes exact setup, stable leg-ID dimensions, disjoint calendar folds, policy, scenarios, data inventory, executable identity and work bounds. The retained content-addressed inventory includes explicit absence. Workers cannot fall through to mutable lake bytes or fetch repairs. Changes require a new linked study. The existing research writer, runtime authority, attempt fencing and bounded shared pool remain the operational boundaries. The API serves guarded commands and read projections; the jobs API carries authorized dispatch tokens. No broker or Clerk authority enters research workers.

Forward folds test a selection procedure whose winners may differ. Their pooled synthetic path adds each fold's P&L increments to the accumulated prior fold P&L; it does not compound independently reset capital. Every fold still admits positions with the same reset starting cash. Pooled capital sufficiency is therefore not claimed, and procedure counts never become one finalist's track record. Finalists freeze before final results are exposed.

Final opening commits shortlist, prior exposure classification, permanent exposure reservation and dispatch intent together. Ordinary Options backtests reserve same-underlying interval exposure before execution; older uninstrumented history is unknown. Command receipts make retry return the original outcome. Cancellation preserves exposure. Worker completion cannot overwrite a newer attempt or a completed result.

The existing immutable Golden Options registry remains the approval authority. A selected complete ordinary run receives its existing input/replay proof. A separate retained study manifest binds the selection history, traces, policy, final comparison and exceptions. Recomputing metrics verifies that manifest before atomic publication. An execution re-proof never silently recertifies an earlier statistical verdict under changed metrics. Approved evidence is not deletable. Approval starts no bot; all existing Deploy, custody and provider decisions remain in force.

## Alternatives

Reusing stock metric and seal types would mix trade units and drawdown conventions. A second session engine would let search approve different rules from execution. Hash-only snapshots lose reproducibility when the lake changes. Publishing the ordinary run alone would prove replay without proving the search qualification claim. Each alternative leaves a materially weaker authority boundary.
