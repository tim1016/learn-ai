# ADR 0080 — Options Golden Search owns selection evidence, existing Options authority owns approval

**Status:** Accepted 2026-10-17. **Authority:** owner implementation request for #3332, including its tightened architecture.

## Decision and why

Options Golden Search composes the canonical Options session rules and historical adapter. It does not register a Signal Program or change Signal Golden Search metrics. Python owns its fixed-capital minute drawdown, calendar-annualized daily Sharpe, recovery, gate applicability and deterministic selection. Replay publishes an optional observation after due fills and same-close decisions; observing never changes a trading decision. Missing contemporaneous held-position marks invalidate qualification (under mark rule v1; see "Missing held-minute marks" below). Incomplete sessions stop research before ordinary diagnostic marked-value cash carry can fund a fictitious next session.

One protocol freezes exact setup, stable leg-ID dimensions, disjoint calendar folds, policy, scenarios, data inventory, executable identity and work bounds. The retained content-addressed inventory includes explicit absence. Workers cannot fall through to mutable lake bytes or fetch repairs. Changes require a new linked study. The existing research writer, runtime authority, attempt fencing and bounded shared pool remain the operational boundaries. The API serves guarded commands and read projections; the jobs API carries authorized dispatch tokens. No broker or Clerk authority enters research workers.

**Versioned session inventory (#3422; 2026-10-20).** A new retained snapshot may name admitted session-container blobs and pinned listing blobs. Its immutable inventory still records unresolved logical contract days as absent; a later lake repair cannot fill them. All container members are validated on retained replay, and a changed blob refuses. Version-1 per-contract snapshots and their retained original bytes remain readable unchanged. The new storage layout changes neither selection rules nor existing evidence identities.

Forward folds test a selection procedure whose winners may differ. Their pooled synthetic path adds each fold's P&L increments to the accumulated prior fold P&L; it does not compound independently reset capital. Every fold still admits positions with the same reset starting cash. Pooled capital sufficiency is therefore not claimed, and procedure counts never become one finalist's track record. Finalists freeze before final results are exposed.

The frozen elapsed-time bound applies separately to each executable stage (research, final, approval). Human review between stages consumes none of the next stage's allowance; retries within a stage share its original deadline and cannot renew an exhausted limit.

Final opening commits shortlist, prior exposure classification, permanent exposure reservation and dispatch intent together. Ordinary Options backtests reserve same-underlying interval exposure before execution; older uninstrumented history is unknown. Command receipts make retry return the original outcome. Cancellation preserves exposure. Worker completion cannot overwrite a newer attempt or a completed result.

The existing immutable Golden Options registry remains the approval authority. A selected complete ordinary run receives its existing input/replay proof. A separate retained study manifest binds the selection history, traces, policy, final comparison and exceptions. Recomputing metrics verifies that manifest before atomic publication. An execution re-proof never silently recertifies an earlier statistical verdict under changed metrics. Approved evidence is not deletable. Approval starts no bot; all existing Deploy, custody and provider decisions remain in force.

## Alternatives

### Amendment 2026-10-20 — Ordinary Options runs share execution capacity

Options Backtest joins Options Golden Search and Signal studies on the same
research pool. Every Options owner counts against the same qualified width;
one is the default until combined workload qualification authorizes more.
Shared execution changes neither selection evidence nor approval authority.
The ordinary run freezes admitted storage answers and original byte pins
before dispatch, retains chronological cash within each chain, and saves only
after every mandatory chain has succeeded. Timings and cache observations
remain outside financial and qualification identities.

Reusing stock metric and seal types would mix trade units and drawdown conventions. A second session engine would let search approve different rules from execution. Hash-only snapshots lose reproducibility when the lake changes. Publishing the ordinary run alone would prove replay without proving the search qualification claim. Each alternative leaves a materially weaker authority boundary.

## Selectable objective — owner decision, 2026-10-17

The owner approved ranking by net profit, daily Sharpe, or lower starting-capital drawdown, independently of the mandatory eligibility limits. The objective is frozen in policy and applies to training selection and development shortlisting; final data never chooses it. Existing protocols retain net-profit ranking and their original identity. Synced timing ranges shift the shared saved entry and exit schedules in whole minutes, relative to their existing calendar anchors, so all legs move together. A failing candidate remains inspectable but cannot become a recommended winner merely by scoring well on the objective.

The owner subsequently approved shared trading-rule exploration by default, with independent per-leg customization. A shared stop-loss, momentum or re-entry rule is one frozen dimension containing a complete stable-leg-ID map per candidate, rather than one dimension per leg; this prevents an unintended Cartesian product. The saved seed can retain different rules per leg, while new shared candidates apply the same rule to every leg. Shared and individual dimensions for the same rule cannot coexist.

Diagnostic rows are bounded to the best 50 rejections under the frozen objective, with the full rejected count exposed separately and complete evaluations retained. They never enter an implicit final shortlist. New protocols freeze candidate identity version 2, which canonicalizes decimal rule values while preserving the saved seed's spelling; older protocols keep version 1 so replay cannot change retained candidate identities. Verification reconstructs the recorded selection-envelope shape, including studies written before rejected diagnostics existed.

## Missing held-minute marks — owner decision, 2026-10-21

The owner chose to let a study continue through a minute in which a held option contract has no bar, valuing it at its previously traded price. That price actually traded and needs no model or bound. This is the explicit decision about what risk evidence qualifies that #3395 Slice 4 (#3404) required before any evaluator change.

A new mark rule, `options-held-minute-close-v2`, beside `options-held-minute-close-v1`, values a held contract that has no minute bar at the close of its most recent earlier minute bar in the same session. It never takes a later minute, an earlier session or another contract; if nothing has traded since the position opened, the mark stays missing and the v1 stop applies. The carried price is for valuation only: fills, stops, momentum and re-entry still read actual bars, observing still changes no trading decision, and a carried price is never a fill or settlement price. The other stops are unchanged: a session that ends with a leg still held, an underlying that did not trade, or invalid capital still stops research, so a carried price cannot fund the next session. Net profit, Sharpe and cash carry still come from actual fills. Drawdown, peak and recovery are computed over the carried values, and a carried minute is no longer a technical failure.

Protocols freeze the mark-rule identifier. Existing protocols, studies, verdicts and approved manifests keep v1 and their identities, and none is re-run or re-certified; a study under `options-held-minute-close-v2` is a new linked study. The policy gate, its limits and the exception rules are unchanged. Every result and study manifest records the identifier and discloses the carried marks, their share of the required held marks and the longest consecutive carried run of any one contract, so a candidate that wins with carried marks is shown as one. Marks are counted per held leg per minute, as the existing required held marks are, so a minute in which two legs are carried counts twice.

A carried price is an assumption, not an observation. A flat line through a gap can understate how far the position moved, so drawdown and recovery over carried minutes can be better than the true figures, most for near-expiry contracts late in the session. No limit on the number or length of carried minutes is set; adding one is a separate owner decision and a new mark rule version, and until then disclosure is the only safeguard specific to gaps. Considered and not chosen: keeping the stop (retained for v1 identities), and pricing each missing minute at a worst-case bound (a bought option no lower than its intrinsic value), which gives a guarantee rather than an estimate but is useful only for defined-risk setups. Implementation is slice Q of #3395.
