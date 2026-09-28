# Deployment fee attribution

The owner policy is [PRD #2540](https://github.com/tim1016/learn-ai/issues/2540), implemented by `alpaca_fee_attribution.py`. This is a custody attribution policy, not a reconstruction of Alpaca's internal allocations. The existing pinned regulatory fee model remains the sole source of unrounded SEC, TAF and CAT weights and dated rates; see [the rate references](alpaca-regulatory-fees.md).

For an observed integer-cent charge C, compute exact quotas C×weight/Σweights. Give each subject the integer part and distribute remaining cents by descending exact remainder and ascending stable custody ID. A five-cent charge with three equal weights becomes 2,2,1. Zero-weight positive charges and unknown rates remain account-unattributed. The independent Fraction oracle under `tests/fixtures/golden/alpaca-fee-attribution` pins exact equality, including fractional-share and sub-cent weights.

Complete effective Clerk fills preserve bot/manual identity and correction lineage. Proven external activity joins as an external subject; an external filled order without its complete activity population makes the answer unknown. Broker activity identity deduplicates delivery. Conflicting evidence is retained and refuses admission; it is not silently replaced. Proven order links attribute directly. Reported fill fees and aggregate charges with unproven overlap remain unresolved. Refunds need the original charge linkage and reverse its allocation; cumulative partial refunds cannot refund a cent twice.

Broker payloads currently provide order IDs but no reliable structured component, refund-of, or fill-fee coverage relationship. Those missing relationships are not inferred from descriptions or matching dollars. The allocator supports explicitly proven relationships; the current ingress honestly leaves unlinked refunds/overlap unresolved.

`FEE_EVIDENCE_OBSERVED` custody transitions retain normalized activity evidence and coverage. A read appends only the rows no earlier record retained (a first delivery, or an economically different copy that the projection treats as a conflict) plus its window facts: oldest dated row, provider exhaustion and read time. An unchanged read appends nothing, so unioning the records equals unioning every full read in order. They replay through the existing mirror; there is no fee balance table. Paper/Live have an independent producer, not a UI-driven cache. Producer freshness is process-local, like the account observation's cash freshness: the repository's `fee_attribution(now_ms=...)` passes the latest successful read time, and a rebuilt or restarted authority refuses until its producer reads again. Reads missing coverage or with stale producer evidence refuse new spending. Dry Run/Shadow ignore real activity evidence entirely and use the same model apportionment.

`custody_fee_attribution(connection, now_ms=..., evidence_checked_at_ms=...)` projects under the caller's SQLite fence. `total_for(subject_id)` includes attributed fees once. `unobserved_cash_claim(cash_seen_before_ms=...)` excludes reported fees already owned by the unseen-fill claim and only reserves activity charges not yet proven in cash. Simulation consumers pass `modelled_fees_seen_before_ms` once modelled settlement is already deducted from their cash projection. Unknown attribution must fail new admission closed. This does not revoke reducing recovery.

Provider pagination reference: [Alpaca Trading API account activities](https://docs.alpaca.markets/us/reference/getaccountactivities-2), checked 2026-09-27, limits page size to 100. The read adapter retains explicit short-final-page proof, preserves malformed/duplicate evidence for attribution, and applies its existing three-page bound. An unfinished window remains unproven; UI does not pretend a bounded read exhausted history.

Partial settlement replaces only the proven fill/order and component scope.
The full-day model is settled once and apportioned to subjects as before; each
subject's component cents are then partitioned among its fill IDs with the same
exact remainder rule. This internal partition only identifies which pending
cents a corresponding charge replaces. It does not round another fee total or
reallocate unrelated subjects when one order settles. Unmatched scopes retain
estimated provisions, including after a refund or a later account-cash read.
An invalid coverage link cannot remove a reported fill fee. A component charge
cannot replace an undifferentiated reported total without component-overlap
proof. Regression cases cover partial orders, components, fills of one subject,
refunds, reported fees, and the persisted custody-to-budget consumer.

A simulated prior-close equity baseline passes `simulated_fill_cutoff_ms` to
select effective fills at or before the canonical calendar close, before the
same model settlement and attribution. Recorded observation time is not an
economic cutoff; correction leaves retain their root execution time. Real
accounts reject this option and retain their existing trade-date windows.
Current execution completeness checks still apply to historical projections.
The before/at/after-close and later-correction regressions cover both Dry Run and
Shadow with exact-cent assertions; reads append no custody transitions.
