# Alpaca live authority — graduation and its residuals

**Status:** ADR 0059 slice 7 (2026-09-09). Lineage: live.
Since PRD #2540 and #2547, a Live deployment needs its own reviewed budget and consent.
Since #2629 the per-instance arming gate is deleted, and the live authority
composes the risk envelope alone (`app/broker/alpaca/clerk/live_authority.py`).

## Graduation

A live account is **shadowed or live-custodied, never both at once** (design
R1): one primary authority per process. On a live-mode boot,
`active_authority.select_active_clerk_runtime` asks the cutover's
`ActivationStore` (`accounts/alpaca/`) for a record naming the observed
account. Present, `live_authority.select_live_clerk_runtime` composes the live
authority; absent, the Shadow Account Authority is composed exactly as slice 4
built it. Graduation is therefore the live cutover. Its normal operator surface
is the account's **Configuration → Graduate Shadow to Live** ceremony. The lane
captures broker evidence itself, verifies the flat/order-free account and stopped
roster, publishes a verified backup, shows the exact short-lived plan, and only
after explicit confirmation appends activation and gracefully restarts under the
deployment supervisor. No browser field supplies a path, mode, evidence file, or
force option.

The equivalent CLI remains the recovery/fallback interface:

```bash
cd PythonDataService
# The evidence file names the account, `"account_mode": "live"`, flat
# positions and no open orders; the CLI refuses live evidence unless
# ALPACA_MODE=live. No shadow receipt is required to graduate: shadow is a
# mode, not a requirement (owner decision 2026-09-09).
python -m scripts.manage_alpaca_sqlite_clerk --account-id <LIVE> --artifacts-root <CLERK_DIR> \
    cutover-initialize --broker-evidence live-evidence.json --max-evidence-age-ms 600000 --runner-artifacts-root <RUNNER_ROOT>
python -m scripts.manage_alpaca_sqlite_clerk ... cutover-plan  --output plan.json ...
python -m scripts.manage_alpaca_sqlite_clerk ... cutover-apply --plan plan.json --confirmation-token <token> ...
```

A never-legacy live account has no legacy JSONL authority to quarantine; live
evidence by itself permits the empty legacy set (R3) — no shadow receipt is
consulted, because shadow is a mode, not a requirement (owner decision
2026-09-09). The account must be **flat and order-free** to graduate — nothing
of ours has ever submitted on it, so any position is a human's; flatten it by
hand first. Stop the shadow instances
before the cutover: after it they are sealed on `shadow:<id>` while the
authority custodies `<id>`, so they are foreign to it — repaired from their own
lifecycle files at boot, refused `SEALED_ACCOUNT_MISMATCH` on any Start (R15).
Graduation has no reversal in this slice (`dev_reset` refuses live by D10); a
`LiveDeactivationRecord` mirroring disarm is the follow-up.

Cold start on the live authority is the paper path's (R18): the cutover's
flat-and-order-free evidence and `recover()`'s reconciliation of open orders
at every boot. The shadow namespace scan is not applied — after the first real
order it would refuse every boot.

A boot Alpaca did not answer is not a failed boot (#2582). When the only
failure behind a selection is Alpaca not answering yet — a network failure,
a timeout or a 5xx (`BrokerUnreachable`), a rate limit (`BrokerRateLimited`,
whose Retry-After is waited out), or startup recovery outrunning its 60 s
deadline (`StartupRecoveryTimedOut`) — the selection ends in
`BROKER_UNREACHABLE_RECONNECTING`, whose copy says it is reconnecting and
needs no restart. The `BrokerUnavailable` catch-all for an answer no mapping
recognized (an unexpected 404 or 409) is not transient. The lane serves with
the refusal installed while `authority_reconnect.py` retries on a capped
backoff (2 s doubling to 60 s, forever: owner decision 2026-09-29), logging
and counting every attempt (`RECONNECT_COUNTERS`). One attempt is the boot's
own steps — select, acknowledge, install, boot recovery — and an installed
authority whose boot recovery fails is retired and replaced by the refusal a
failed boot composition installs (`compose_failure_refusal`): reconnecting
when Alpaca was the cause, final with copy that says restart otherwise, and
naming the same activation either way, so Home's account line and the account
panels keep naming it. A reconnect that breaks on an unexpected error installs
a final refusal that keeps that activation too, and boot recovery reruns for
every refusal so Start reads a finished report. Every other startup failure
stays final, and its copy says so and names the fix. Routed reads tell the same truth from the lane's beat:
its summary reports `authority_state=reconnecting`, and the coordinator
authors the 503's copy from the session it routed through.

### After graduation: the rehearsal's bindings are foreign, not corrupt (R15)

A binding sealed on a custody account the installed primary authority does
not custody — after graduation, the rehearsal's `shadow:<live_account_id>`
bindings under the live primary — is *foreign* to that authority, not
corrupt. `bot_boot_recovery.py` decides that from the registry's own routing
(the binding's authority is the primary lifecycle authority) plus the same
comparison Start makes (`binding.sealed_account_id != <installed Clerk
account id>`), so a `sim:` Dry Run binding — routed to its own per-instance
authority — is never foreign. Such a binding is named in the boot report's
`foreign_instances`, logged once (`action=boot_recovery_foreign_binding`),
and **left exactly as its own lifecycle files say**: never projected against
an authority that has never seen it, no duty state authored from the sweep
(ADR 0050), and the sweep never aborts. Unlike
`authority_unavailable_instances` this does not close the Start gate —
foreign bindings are refused one at a time on Start with
`SEALED_ACCOUNT_MISMATCH` (`run_admission.py`), and native instances stay
startable. The live verdict's arming count reads the same rule: a
`shadow:`-sealed instance is not armed on the graduated account.

## Residuals

- One primary authority per process (R1): a graduated account cannot shadow
  a new instance until a secondary-authority seam (a shadow facade per
  `shadow:` binding beside the live primary, the way `sim:` is selected per
  instance) lands — a named follow-up slice, deferred by the owner on
  2026-09-09. Rehearsal is optional, so this blocks nothing.
- Graduation has no reversal (R1).
- The halt writes no desired state (R10) — owner question E1.
- A refused live boot labels its compat surfaces "paper" (`custody_world_or_paper`).
- `StrategySpec.submit_mode` is not stamped at deploy — E8.
- **Historical execution recovery stays paper-only.** `historical_execution_recovery.py`
  refuses `LIVE_ACCOUNT_REFUSED` at both entry points; re-meaning it for a live
  account (its own admission and a live-safe replay) is a follow-up slice.

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decisions 1, 3, 8, 10, 11 and Consequence 7. Controller rulings R1–R18 were in
the slice-7 design spec (Git history).
Predecessors: [alpaca-shadow-authority](alpaca-shadow-authority.md),
[alpaca-live-envelope](alpaca-live-envelope.md), [alpaca-live-arming](alpaca-live-arming.md).
