# Broker clerk fleet authority

**Status:** canonical. **Written:** 2026-09-14. **Domain:** the multi-broker clerk fleet control
plane, and current Alpaca Broker V2 operating behaviour.

## What this document is

What the code cannot say about the fleet control plane, from the investigation charted at
[#2052](https://github.com/tim1016/learn-ai/issues/2052): the trust risk register and its standing
decision (§6), the operator-visible gaps (§7), where the code departs from the accepted authorities
(§8), and the operator vocabulary two ADRs point at (§9). The control plane itself is
[ADR 0062](architecture/adrs/0062-broker-clerk-fleet-control-plane.md),
[ADR 0063](architecture/adrs/0063-draining-is-an-observed-lane-handover.md) and
`app/broker/fleet/`. Sections 1–5 and 10 restated them and were cut; Git history has them.

**The evidence bar it was written to:** code is authority; documentation is a claim. Where an
accepted authority — ADR 0062, the PRD, `CONTEXT.md` — says something the code does not do, that
divergence is recorded as a finding rather than smoothed over. §8 collects them.

**It describes; it does not decide.** Decisions live in ADRs, open defects in
`docs/known-gaps.md`, procedures in `docs/runbooks/`. This document cites all three and duplicates
none. That is what keeps "one authority" true after the day it was written.

**Citation convention.** Paths are relative to `PythonDataService/` unless they begin with
`Frontend/`, `docs/`, `scripts/` at repo root, or a compose file.

**What was not done:** the fleet was not executed. No containers were started, no lanes booted, no
fences exercised under fault. That bar was sufficient — the ADR audit found nothing that reading
could not settle — but it bounds §6 and §7, and the bound is stated there.

## Accepted authorities this describes

| Authority | What it owns |
|---|---|
| [ADR 0062](architecture/adrs/0062-broker-clerk-fleet-control-plane.md) (Accepted 2026-09-12, + A1 addendum 2026-09-13) | The decision and its intent |
| `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)" | Vocabulary — adopted here unchanged |
| [ADR 0059](architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md) / [ADR 0060](architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md) | Deployment consent, effective account risk and profile changes, retained per clerk |

The Alpaca clerk's internals — custody, budgets, recovery — are described **at their interface
only**. They carry their own accepted authority in ADRs 0035 / 0037 / 0047 / 0059 / 0060.

---

## 6. Trust risk register

Full reasoning and the owner's recorded appetite:
[#2059](https://github.com/tim1016/learn-ai/issues/2059). This is a **register, not a gate**.

**Standing decision (2026-09-14): proceed. Run bot launches through the fleet on the Live lane.**

The register splits three ways, not the expected two:

| Axis | Level | Why |
|---|---|---|
| **Structural** — can it keep lanes apart as built? | **Low** | Identity is not a request parameter; the fences are in the database and tested; ADR 0062 scores **0 absent**; of the four partial decisions tracked when this was written, three are now closed and Decision 5 remains partial (§8) |
| **State** — is what runs what was designed? | **High** | Rated when the running posture lived only in a gitignored `compose.override.yaml` and `restart.sh` could destroy a lane. Both were fixed on 2026-09-14: the topology is committed as `compose.fleet.dev.yaml` (`6da8cd61`) and `restart.sh` classifies containers by Compose label (`563c5bab`). The level was not re-rated |
| **Observability** — if it goes wrong, will you know? | **High** | No audit read surface (§7). Rated when no refusal code rendered; they now render through `Frontend/src/app/fleet/fleet-refusal-copy.ts` (#2067) |

The third axis was not anticipated when the map was charted; the investigations forced it out, and
it is the one worth acting on first. **You can accept structural risk when you can see failures.**
When this was written you largely could not: a refused launch showed a blank or generic error.
Refusals now render (#2067), but the diagnosis behind them still sits in a SQLite file that has no
route.

### Why proceeding is nonetheless right

**Paper is dark** (on 2026-09-14 the Paper lane was `ACTIVATION_REQUIRED`). Every **cross-lane**
risk here — two lanes racing a reservation, wrong-lane delivery, the shell showing one lane's
verdict as the installation's, identical fake canonicalization hiding a provider-qualification bug — is therefore **latent, not live**.

**The day the Paper lane activates, that block flips from latent to live, and this register should
be re-read before it does.**

The realistic bad night is not a silent wrong-account trade — depth enforcement is real and tested
— it is **a refusal you cannot read**, or **a clerk destroyed by `restart.sh`**. Both recoverable;
both cheap to fix, and both since fixed (#2067, `563c5bab`).

### What would change the answer

1. Paper lane activation.
2. Browser-driven provisioning, or a second concurrent operator — the volume-root fence is now
   transactional and DDL-backed, so the registry itself no longer has a race here; the
   remaining exposure is whatever a second operator does outside the registry, which this
   document has not audited.
3. Loss of `compose.override.yaml` — moot since the topology was committed (`6da8cd61`);
   fleet credentials still live only in the gitignored `deploy/fleet/env/*.env`.
4. A real additional broker — both fakes canonicalize identically
   (`tests/broker/fleet/conftest.py:127`), so provider-qualified assignment is effectively
   untested.

### Ranked trust-raising changes

| # | Change | Axis |
|---|---|---|
| 1 | Fix `restart.sh`'s five-name allowlist (`:28`, `:64-65`) | State |
| 2 | Fix the three refusal parsers to read the flat body; render the 25 reason codes | Observability |
| 3 | Commit the fleet topology with credentials externalised | State |
| 4 | Run `run_host_qualification` once on the host | State |
| 5 | Give `FleetDirectoryService.refresh()` a caller; freeze binding generation at action open | Observability |
| 6 | Close the unscoped-mutation retirement hole (§8) | Structural |

Items 1, 2, 3 and 5 have since landed (`563c5bab`, #2067, `6da8cd61`, #2068).

Item 6 is where **an accepted authority asserts something the code does not do**.
Resolution is acceptable; leaving it is not.

## 7. The operator-visible surface, and its gaps

Full catalogue: [#2057](https://github.com/tim1016/learn-ai/issues/2057).

**The surface is asymmetric.** The *request* path is rigorously modelled — 13-member capability
vocabulary, 80-operation typed catalogue, per-command generation fence, pre-dispatch routing
receipt. The *failure and evidence* path stops at the Python boundary.

The gap list is deliberately kept as **two halves that are never merged**, because they are
different kinds of work. Half A is pure frontend with no protocol risk; half B hides at least
three wire-contract or semantics changes that need an owner decision before any pixel is designed.
**There is deliberately no priority column** — priority is the reader's to set.

### Half A — the backend has it; no UI renders it

- **The durable audit trail has no read surface.** Routing receipts, assignment history and
  session history are append-only, trigger-protected, and reachable only by opening the
  coordinator's SQLite file by hand. `aggregate_lane_reads` (PRD FR-083/084) now has an HTTP
  route — `GET /api/broker-clerks/aggregate/directory`, via
  `FleetControlService.aggregate_directory_reads()` — a resilient, per-lane-isolated twin of
  `GET /api/broker-clerks`; no frontend consumer was built (deliberately, per the closed
  merged-roster-UI decision).
- **`X-Fleet-Correlation-Id` and `X-Fleet-Routing-State`** are written on every command and read by
  nothing.
- **`authority_state`** is carried to the browser and never rendered.

### Half B — a UI needs it; the backend lacks it

- **`ready` does not mean "able to trade."** A lane with `shadow`, `synthetic` or `unavailable`
  authority still projects `ready`. Whether `ready` means *addressable* or *able to trade* is an
  owner decision, not a UI choice.
- **48% of refusal sites carry no `next_step`** (85/176), including `clerk_unreachable` — 13 raise
  sites, zero next steps.
- **Auth and infra refusals break the `{reason, message, next_step}` contract** — 403s and "no
  registry installed" return a bare `{"detail": …}`.
- **`lifecycle_state` is typed `string`** and only `'ready'` is ever compared.
- **Refusal reasons are not in the exported OpenAPI contract**, so any frontend union is
  hand-maintained and will drift.
- **Seven fleet modules emit no logs whatsoever**, and the enrolment/retirement ceremonies are
  CLI-only. Whether ceremony *evidence* should be addressable is undecided.

## 8. Divergences from the accepted authorities

Where a document claims something the code does not do. **The code is authority; these are the
claims to fix.**

| Claim | Where | Reality |
|---|---|---|
| Decision 2: nothing opens on the volume before the identity gate | ADR 0062; comment at `app/main.py:383-386` | **Holds.** The profiles-DB writer and the installation lock file now open only after the gate. `fleet_boot.py:159-162`'s registry open still precedes it, but that registry lives on the coordinator's **control** volume, not the clerk's lane volume — the gate has to open the registry it is checking the lane volume against, so this one is structurally unavoidable, not a violation |
| Decision 5: provider safety gates answer before any mutation | ADR 0062 | Dispatched now at the routing seam (`LaneRouter._resolve` in `app/broker/fleet/routing.py` calls `validate_served_context` for execution operations) — but the Alpaca adapter declares no refusal the seam can currently trigger. The gate is structurally live and semantically empty for the only production provider (owner decision pending) |
| The coordinator "never serves an unscoped agent family itself" | `CONTEXT.md:2121` | It serves two unscoped agent-family reads |
| "The browser secret terminates at the coordinator" | `CONTEXT.md`, composed-auth bullet | **Holds for mutations** since #2075: a `clerk_agent` refuses any mutation that is not the coordinator's authenticated forward (`refuse_unpinned_mutations` in `app/broker/fleet/agent_identity.py`). A browser-direct **read** still passes on the browser secret, so for reads it is compose network topology (`compose.fleet.yaml`), not code |
| Retained unscoped **mutations** will retire with the compatibility mechanism | The fleet-B route inventory (in Git history) | `_SAFE_METHODS = frozenset({"GET", "HEAD"})` (`app/broker/fleet/lane_runtime.py:42`) — the mechanism **can never reach mutations**. Retirement is also all-families-or-nothing despite a per-family constant, and the gate is clerk-agent-only, so it cannot reach the browser's actual compatibility reads |

### What the test suite proves, and does not

The fleet suite is a strong **contract-and-schema** suite wearing a **concurrency** suite's
vocabulary: 6 of 227 tests create real contention, and only 2 contend across separate store
connections. Roughly 37 tests have names that diverge from their assertions — including
`test_compatibility_evidence_separates_failed_probes_from_successful_reads`, which proves the
two **collapse**. Full assay: [#2056](https://github.com/tim1016/learn-ai/issues/2056).

## 9. Operator vocabulary

Absorbed on 2026-09-14 from the retired operator manual, because two Accepted ADRs name the
operator-facing document as the place these terms are defined. Without this section their
definitions would exist nowhere an operator is told to look.

### Deploy, Stop and remaining money

The account-scoped Deploy page is the single entry point for a fresh run. It
reviews the qualified configuration, server-priced dollar budget, immutable
exit terms and current account risk policy. Live also requires typed consent.
Paper offers Paper and Dry Run; a Live lane offers Shadow and Dry Run before
graduation, then Live and Dry Run afterward. Configuration owns Shadow
activation, graduation, Apply risk limits, guarded hold clearance and the
explicit Budget authority upgrade for existing accounts.

Stop ends new strategy decisions. It releases proven free cash while pending
orders, unseen fills, fees and positions remain attributed to the stopped run.
Use the existing Reconcile and guarded Flatten actions to resolve those
obligations. Deploy again opens a fresh Deploy form and grants no permission
itself. Pause, Continue, Resume and standalone arming are retired. Historical
evidence remains readable; it cannot create a new run or a dollar commitment.

One Clerk projection supplies the displayed budget and cash claims and checks
new entries. Apply risk limits changes the effective account policy without
redeploying bots; it cannot rewrite their exit terms or silently clear a hold.
Shadow and each private Dry Run retain their own cash, fees and risk evidence.
See [ADR 0059](architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
and [ADR 0060](architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md).

### `corpus_coverage` — qualification for the exact configuration
*(home for [ADR 0054](architecture/adrs/0054-corpus-coverage-is-a-stamp-on-paper.md),
as amended by #2544)*

The golden corpus behind a program's `golden_trace_root` covers a particular
symbol and exact parameter values. The registry qualifies its
`validated_settings` over its `validated_symbols`; an unsupported tuple is
`UNCOVERED` even when the artifact and wiring digests match.

Paper, Shadow and Live require a covered tuple before a deployment can commit
money. Deploy shows that tuple and offers **Use qualified configuration** or
**Try other settings in Dry Run**. It never silently changes the configuration or self-certifies
a new corpus point. Dry Run retains its explicit exploratory exemption; its
frozen evidence keeps the coverage result and cannot claim qualification.

Corpus coverage, applicable human promotion, program access and operational
readiness remain separate facts. A Manual override belongs to the existing
human-promotion authority and does not bypass corpus, cash or account risk.
Known affected evidence requires review; unknown provenance is shown honestly.
Historical acceptances remain immutable. No strategy-evidence gate is added
at ENTER.

### `account_mode`
*(home for [ADR 0054](architecture/adrs/0054-corpus-coverage-is-a-stamp-on-paper.md))*

The environment (`paper` / `live`) on the Clerk custody snapshot, positively learned from the broker
at activation. **Required, never unknown** — it fails closed rather than defaulting.

### `FEED_DEATH` typed reasons
*(home for [ADR 0053](architecture/adrs/0053-feed-continuity-same-run-recovery.md); see also
`docs/references/feed-reconnect-continuity.md`)*

`FEED_DEATH` is the reason code a `CRASHED` duty outcome carries when the run's market-data stream
ended. Since #1921 a broker-socket interruption is not automatically fatal for a run carrying a
continuity policy — a sealed regular-session program with a decision clock, while the data plane's
feed-continuity switch is on. Such a run waits the reconnect out under its own decision clock and
stitches the interrupted minute back together, so when it *does* report `FEED_DEATH` it says which
continuity rule refused it. The typed reason prefixes the crash diagnostic's message
(`"<REASON>: …"`) and is a column on the matching `refused` row in the run's continuity journal.

The vocabulary is closed:

| Reason | Meaning |
|---|---|
| `DECISION_BAR_MISSED` | The socket did not return before the deadline for the run's next decision bar (its trigger instant plus a 20-second delivery allowance). The run stopped rather than decide late; the `interruption` event records the deadline it was held to. |
| `SUBSTITUTION_NOT_AUTHORIZED` | A minute an interruption touched could not be proven complete from live data, and nothing authorizes standing a historical bar in its place. Nothing authorizes it in this build, for any instrument or program — this is the expected reason for a print an interruption lost inside regular trading hours. A short minute with no interruption behind it is `MINUTE_INCOMPLETE` instead. |
| `MINUTE_INCOMPLETE` | A regular-session minute no interruption touched held fewer than the twelve 5-second prints the calendar says it owes — the line went quiet for under 60 s with no connectivity notice (#2364). No interruption episode exists, so no substitution is asked about; the run stops rather than decide on a possibly truncated bar. A half-day's early close ends the owed count where the session ends. |
| `SUBSTITUTION_PATH_UNAVAILABLE` | An authorization was granted but no substitution path exists to honour it. No producer of such a grant is deployed, so this means one appeared without its delivery half — **escalate rather than retry**. |
| `CONTINUITY_EVIDENCE_UNWRITABLE` | A continuity fact could not be written to the run's ledger. The run stops rather than continue without the evidence it promised. Check the account's storage and the ledger file before redeploying. |
| `DECISION_LATE` | A bar assembled across the reconnect *was* delivered, but past the allowance for the decision it would have driven. Deciding on it would price a trade against a market that had already moved. |

A run **without** a policy — unsealed or compatibility-mode strategy, an all-session binding, a
program with no decision clock, or any run while the switch is off — keeps the pre-#1921 behaviour:
the first interruption ends it with a plain `FEED_DEATH`, no typed reason and no `refused` row. The
notice's message tells the two apart. The one typed reason such a run can carry is
`MINUTE_INCOMPLETE`, with no `refused` row because it has no journal.

None of these is an operator action. Each is a completed, evidence-backed stop: read the notice,
then redeploy through the panel's normal admitted action once the feed is healthy.

Two data refusals sit outside this vocabulary because no decision was ever at risk of being made on
bad connectivity: a run that never started deciding is recorded under its own duty-outcome reason
code rather than `FEED_DEATH`. A warmup that could not be fetched or covered refuses as
`WARMUP_HISTORY_UNAVAILABLE` / `RESUME_HOLE_AFTER_HOURS` / `RESUME_HOLE_UNFILLED` (#2365, #2314), and
a bar whose values cannot be real — a non-finite or non-positive price, a bar with high below low or
an open or close outside the low–high range, or a negative volume — refuses as
`IMPOSSIBLE_SOURCE_BAR` (#2444), identically on the live subscription and on the warmup history
fetch. `FEED_DEATH` would point the operator at a running bot's stream or at connectivity; these say
the data itself is the problem, and `IMPOSSIBLE_SOURCE_BAR` is not retryable — check the broker's
data quality before redeploying.

## Related

- Runbooks: `docs/runbooks/fleet-dev-two-lane-posture.md`,
  `docs/runbooks/fleet-d-two-clerk-rollout.md`, `docs/runbooks/fleet-d-recovery-and-rollback.md`,
  `docs/runbooks/fleet-e-compatibility-retirement.md`,
  `docs/runbooks/fleet-directory-unavailable.md`
- Moving the installation to another Mac (export, import, go-live, going back):
  `docs/runbooks/migrate-installation.md`
- Alpaca clerk recovery: `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md`
- Open defects: `docs/known-gaps.md`
