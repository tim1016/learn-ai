# Broker clerk fleet authority

**Status:** canonical. **Written:** 2026-09-14. **Domain:** the multi-broker clerk fleet control
plane, and current Alpaca Broker V2 operating behaviour.

**Replaces** `docs/broker-v2-operator-manual.md`, which was retired together with its served copy
and its generator (decision: [#2060](https://github.com/tim1016/learn-ai/issues/2060)). There is
one authority for this domain and this is it.

## What this document is

A description of the fleet control plane **exactly as implemented**, produced by the investigation
charted at [#2052](https://github.com/tim1016/learn-ai/issues/2052).

**The evidence bar it was written to:** code is authority; documentation is a claim. Every
statement here cites `file:line`. Where an accepted authority — ADR 0062, the PRD, `CONTEXT.md` —
says something the code does not do, that divergence is recorded as a finding rather than
smoothed over. §8 collects them.

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
| [PRD](prds/2026-09-12-multi-broker-clerk-control-plane.md) | Requirements |
| [Delivery review](design/2026-09-13-clerk-fleet-delivery-review.md) | Delivery shape |
| `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)" | Vocabulary — adopted here unchanged |
| [ADR 0059](architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md) / [ADR 0060](architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md) | Arming/envelope and Stage→Apply→restart, retained per clerk |

The Alpaca clerk's internals — custody, arming, recovery — are described **at their interface
only**. They carry their own accepted authority in ADRs 0035 / 0037 / 0047 / 0059 / 0060.

---

## 1. The control plane

Three process compositions, selected by one setting. `ROLE` defaults to **`combined`**
(`app/config.py:28`), and `app/main.py:108-115` branches composition on it.

| Role | What the process is |
|---|---|
| `combined` | The legacy posture — byte-for-byte the historical single process |
| `fleet_coordinator` | Data-plane core + fleet registry + `/internal/fleet`; no broker client, clerk authority, stream or bot runner |
| `clerk_agent` | One lane: the Alpaca boot, its streams, its bot runner, under the deployment's approved endpoint |

**The default opts out of every fleet fence.** With `ROLE == "combined"` and a volume that carries
no identity marker, `app/broker/alpaca/clerk/fleet_boot.py:108-116` logs *"running the legacy
unfenced posture for this boot"* and returns `None` — no marker gate, no registry, no
`/api/broker-clerks`. A `clerk_agent` with no marker refuses instead
(`fleet_boot.py:118-122`), which is the addendum's intent; `combined` predates it and is exempt.

This matters more than it reads: **a fresh clone of this repository runs unfenced, and says
nothing at all.** With no fleet configuration the function returns `None` at `fleet_boot.py:110`
without logging; the `INFO` line at `:112-114` fires only in the narrower case where fleet control
*is* configured but the volume is unenrolled. See §5.

## 2. Identity

Five identities, each fencing a different thing. All are backend-issued and opaque; callers never
mint, parse or infer them.

| Identity | Fences |
|---|---|
| `clerk_id` | The durable execution lane. Retirement is terminal; IDs are never recycled |
| `volume_id` | The physical mounted root, plus a nonsecret mount attestation. A copied or mis-mounted root fails closed |
| `worker_key` | The per-clerk worker. Never crosses the public API and never authenticates a transport |
| Routing epoch | One agent registration. A retry must not silently cross an epoch change |
| Effective binding generation | The clerk-local counter that changes when profile, revision or account changes. Commands carry the generation they were prepared against |

### Where identity stops being explicit

**`clerk_id` is explicit only down to the coordinator's dispatch.**
`DeliveryRequest.agent_path()` (`app/broker/fleet/delivery.py:78-80`) binds the operation's
*agent* path template, which carries **no clerk segment**. Below dispatch, the clerk is a header
that `FleetIdentityMiddleware` **compares against** the process's own boot-fixed identity rather
than selects with.

This is a strength, not a gap, and it is worth stating plainly because it inverts the obvious
reading: lane identity below dispatch is carried by three *process-global* facts — the installed
binding, the installed clerk runtime, and the fenced artifacts root — and enforced in depth by
`SealedAccountMismatchError`. **A process cannot be talked into serving the wrong account by a
crafted request, because the account is not in the request.** That is strictly stronger than
parameter validation.

The cost is that the *selection* has moved into deployment topology. See §4.

## 3. The request path

1. **Operation catalog** — 80 typed provider-owned declarations (method, public and agent path
   templates, capability, readiness, account requirement, idempotency kind, stream kind). It is
   the single source for the coordinator's forwarding allowlist, the agent's mounts, the exported
   OpenAPI and the generated frontend builders.
2. **Capability** — a closed 13-member typed vocabulary. An undeclared action is a typed refusal,
   never an emulation or a cross-provider parity assumption.
3. **Command envelope** — every effectful public request carries `command_context`: capability,
   idempotency key, expected binding generation, target. Capability must match the routed
   operation; a durable-keyed operation requires its key; there is no implicit canonical command.
4. **Routing attempt** — one coordinator-side delivery attempt with its pinned context persisted
   **before** dispatch. Outcomes: `not_dispatched`, `provider_refused`, `delivered`, or
   `outcome_unknown` (reconcile by identity, never auto-resubmit). The provider clerk stays the
   sole deduplication and outcome authority.
5. **Identity echo, per event** — streamed responses stamp every SSE frame with the serving
   runtime's broker/clerk/epoch/generation, read live at frame time; the coordinator closes the
   stream on the first violation.

**Readiness** is either `configuration_access` (routable for an unbound lane, so its configuration
can be repaired) or `execution` (requires the confirmed binding). §7 records a defect in which the
first of those becomes unreachable exactly when it is needed.

The three account-scoped Live-graduation operations are intentionally separate from
configuration Apply: status reads the boot-selected authority; plan captures lane-owned
broker evidence and publishes a verified backup; apply confirms the content-addressed plan,
then returns a durable activation receipt before the clerk's supervised restart. They deploy
and arm no strategy. ADR 0059's 2026-09-17 amendment owns the decision and the focused cutover
runbook owns the procedure.

## 4. The fences — what actually keeps lanes apart

Distinguishing the fences that are **real and tested** from those that are **declared**.

### Real, and tested

- **The registry DDL.** Composite primary key, partial `UNIQUE` indexes, and 13 `RAISE(ABORT)`
  triggers. This is what holds the line under contention, and it is well covered
  ([#2056](https://github.com/tim1016/learn-ai/issues/2056)).
- **Depth enforcement of account identity** — `SealedAccountMismatchError`, behind the installed
  binding and the fenced artifacts root (§2).
- **Volume identity gate** — verified before the broker client opens (`app/main.py:391-393` →
  `fleet_boot.py:185`, ahead of `:409`). ADR 0062 Decision 2 **holds**: the broker-client half
  exactly, and the database half now too — the profiles-DB writer and the installation lock file
  open only after the gate. See §8's Decision-2 row for the one exception, and why it is
  structural rather than a violation.
- **Route-level identity freezing in the browser.** Broker, clerk, account and entity are fixed by
  the route itself, through one URL seam and one immutable `ResourceTarget` — the strongest
  available form ([#2058](https://github.com/tim1016/learn-ai/issues/2058)).
- **A1, A2 and A7 addendum clauses** — all hold, with real triggers and real tests
  ([#2054](https://github.com/tim1016/learn-ai/issues/2054)).

### Declared — three since closed, one still hollow

- **`validate_served_context` had no production caller; it is now dispatched.** ADR 0062
  Decision 5's provider safety gate is declared (`app/broker/fleet/provider.py:283`) and
  implemented (`app/broker/alpaca/clerk/fleet_adapter.py:756`). `LaneRouter._resolve`
  (`app/broker/fleet/routing.py:469-531`, the `if assignment is not None:` block) now builds a
  `ServedContext` from the resolved assignment and calls it for execution operations; a provider
  refusal surfaces as `BrokerClerkCapabilityUnavailable` (409) and is logged with
  `action=fleet_provider_refused_served_context`. The mechanism is real and tested. What it guards
  is not: the Alpaca adapter — the only production provider — currently declares no refusal this
  seam can trigger (§8).
- **The nested/shared volume-root refusal was non-transactional; it is now backed by DDL.** The
  Python pre-check in `provision_clerk` (`app/broker/fleet/service.py`) now runs inside the
  provisioning `BEGIN IMMEDIATE` transaction, backed by schema v3's partial `UNIQUE` index
  `ux_clerks_volume_root` and the `BEFORE INSERT` trigger `trg_clerks_volume_root_not_nested`. The
  race is tested (`test_two_racing_provisionings_of_nested_roots_cannot_both_land`).
- **The volume gate was caller-opt-in; `LocalPresence` now requires and re-proves it.**
  `LocalPresence.__init__` requires `volume_root` and re-proves it on register and reserve
  (`test_local_presence_reverifies_the_volume_at_registration_and_reservation`). The
  `volume_root: Path | None = None` parameters on the service's register/reserve stay optional by
  design — `RemotePresence` cannot inspect an agent-local path (ADR 0062 addendum 6).
- **`_fence_writable_roots` had no negative test; it now does** (commit `bc87bfac`) — deleting the
  fence body fails it.
- **"The browser secret terminates at the coordinator" is documentation, not code.** An unpinned
  request skips `FleetIdentityMiddleware` entirely
  (`app/broker/fleet/agent_identity.py:211-215`) and the agent accepts the browser control secret
  — pinned by a test asserting `200` (`app/routers/data_plane_control.py:83-102`). What actually
  prevents browser-direct mutation of a clerk agent is **compose-file network topology**
  (`compose.fleet.yaml:97-100`, `:48-49`), not code.

### The fault matrix has never been run

`run_host_qualification` (`scripts/run_broker_fleet_compose_qualification.py:714`) contains real
`compose kill` and marker-poison injection. It is called by **zero tests and zero CI workflows**,
and its own test asserts a 5-string tuple equals itself.

## 5. Deployment posture — what actually runs

**As of 2026-09-14, on this host** ([#2055](https://github.com/tim1016/learn-ai/issues/2055)):

The isolated posture **is** in effect. `polygon-data-service` runs `FLEET_ROLE=fleet_coordinator`;
`alpaca-live-clerk` and `alpaca-paper-clerk` run at `clerk_agent`, each on a distinct named volume
with a valid `.learn-ai-clerk-volume.json` marker. ADR 0062 Decision 2's safety argument is live,
not theoretical.

**And it is delivered entirely by a gitignored `compose.override.yaml`**, which is also the sole
durable copy of every fleet credential.

Consequences, stated plainly:

- **Nothing committed records what runs.** There is no `FLEET_*` anywhere in `compose.yaml`. Lose
  the untracked file and the installation reverts to `combined` — which §1 shows is completely
  unfenced — and says so only at `INFO`.
- **The reviewed overlay has never been deployed; the deployed overlay has never been reviewed.**
  `compose.fleet.yaml` has only ever run inside throwaway `fleetqualification…` projects; zero
  `learn-ai-fleet_*` volumes exist.
- **The running clerks lack the reviewed containment**: `ReadonlyRootfs=false`, `PidsLimit=0`, no
  tmpfs, all three containers on the shared `app-network` rather than `fleet-private`, and host
  source bind-mounted into the container holding Live credentials.
- **`restart.sh` will destroy a stuck clerk.** Its rescue block (`restart.sh:28`, `:64-65`)
  carries a pre-fleet five-name allowlist; a clerk in `Created` — exactly the
  `depends_on: service_healthy` case the block exists for — is `rm -f`'d and not restarted.
- **The coordinator logs a warning per heartbeat** (~12/min). `app/main.py:969` installs
  `FleetIdentityMiddleware` on every role, and `app/broker/fleet/presence.py:230-235` pins
  `X-Fleet-Clerk-Id` on every agent→coordinator call. The message is also wrong for that direction.
- **Nothing in CI renders `compose.fleet.yaml`.**
- **The Paper lane is `ACTIVATION_REQUIRED`** — the two-lane posture is operationally
  one-and-a-half lanes. This is load-bearing for §6.

**Important caveat about this section.** Because the posture lives in an untracked file, §5 is the
part of this document most likely to go stale, and **nothing will say so when it does.**

## 6. Trust risk register

Full reasoning and the owner's recorded appetite:
[#2059](https://github.com/tim1016/learn-ai/issues/2059). This is a **register, not a gate**.

**Standing decision (2026-09-14): proceed. Run bot launches through the fleet on the Live lane.**

The register splits three ways, not the expected two:

| Axis | Level | Why |
|---|---|---|
| **Structural** — can it keep lanes apart as built? | **Low** | Identity is not a request parameter; the fences are in the database and tested; ADR 0062 scores **0 absent**; of the four partials §4 tracked, three are now closed and Decision 5 remains partial (§4, §8) |
| **State** — is what runs what was designed? | **High** | The posture is untracked, unreviewed, unreproducible, and `restart.sh` can destroy a lane (§5) |
| **Observability** — if it goes wrong, will you know? | **High** | 25 refusal codes, none renderable; no audit read surface (§7) |

The third axis was not anticipated when the map was charted; the investigations forced it out, and
it is the one worth acting on first. **You can accept structural risk when you can see failures.**
Today you largely cannot: the most likely operator experience of a refused launch is a blank or
generic error, with the diagnosis sitting in a SQLite file that has no route.

### Why proceeding is nonetheless right

**Paper is dark** (§5). Every **cross-lane** risk here — two lanes racing a reservation,
wrong-lane delivery, the shell showing one lane's verdict as the installation's, identical fake
canonicalization hiding a provider-qualification bug — is therefore **latent, not live**.

**The day the Paper lane activates, that block flips from latent to live, and this register should
be re-read before it does.**

The realistic bad night is not a silent wrong-account trade — depth enforcement is real and tested
— it is **a refusal you cannot read**, or **a clerk destroyed by `restart.sh`**. Both recoverable;
both cheap to fix.

### What would change the answer

1. Paper lane activation.
2. Browser-driven provisioning, or a second concurrent operator — the volume-root fence is now
   transactional and DDL-backed (§4), so the registry itself no longer has a race here; the
   remaining exposure is whatever a second operator does outside the registry, which this
   document has not audited.
3. Loss of `compose.override.yaml` — silent, and nothing today would notice.
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

- **25 refusal reason codes exist; `0` appear anywhere in `Frontend/src/`.** 22 are declared as
  `reason: ClassVar[str]` in `app/broker/fleet/errors.py` (including the `fleet_control_error`
  base); three more are minted inline — `command_envelope_invalid`
  (`app/routers/broker_clerks.py:87`), `compatibility_retirement_state_invalid`
  (`app/broker/fleet/lane_runtime.py:534`) and `compatibility_read_retired` (`:553`).
- **No PRD §10.4 refusal family can render at all.** The fleet returns a **flat**
  `{reason, message, next_step}` — `FleetControlError.detail()` builds exactly that
  (`app/broker/fleet/errors.py:36-40`) and `app/routers/broker_clerks.py:78-88` returns it
  unwrapped. All three frontend parsers read `error.error.detail.*` (e.g.
  `Frontend/src/app/components/broker/v2-panel/lib/panel-action-outcome.ts:21-23`). **The backend
  is correct and self-consistent; the defect is entirely in the frontend parsers.** The rendering
  path beneath them is already right. (The method being *named* `detail()` is an easy trap — it
  returns the body; it does not nest under a `detail` key.)
- **The durable audit trail has no read surface.** Routing receipts, assignment history and
  session history are append-only, trigger-protected, and reachable only by opening the
  coordinator's SQLite file by hand. `aggregate_lane_reads` (PRD FR-083/084) now has an HTTP
  route — `GET /api/broker-clerks/aggregate/directory`, via
  `FleetControlService.aggregate_directory_reads()` — a resilient, per-lane-isolated twin of
  `GET /api/broker-clerks`; no frontend consumer was built (deliberately, per the closed
  merged-roster-UI decision).
- **`X-Fleet-Correlation-Id` and `X-Fleet-Routing-State`** are written on every command and read by
  nothing.
- **Two diagnostic facts are computed, handed to the adapter, then dropped before the wire** —
  `confirmed_by_current_session` and `multiple_effective_assignments`. They are the only facts
  distinguishing the two meanings of `starting` and the two of `degraded`.
- **`authority_state`** is carried to the browser and never rendered.

### Half B — a UI needs it; the backend lacks it

- **`ready` does not mean "able to trade."** A lane with `shadow`, `synthetic` or `unavailable`
  authority still projects `ready`. Whether `ready` means *addressable* or *able to trade* is an
  owner decision, not a UI choice.
- **48% of refusal sites carry no `next_step`** (85/176), including `clerk_unreachable` — 13 raise
  sites, zero next steps.
- **Auth and infra refusals break the `{reason, message, next_step}` contract** — 403s and "no
  registry installed" return a bare `{"detail": …}`.
- **`lifecycle_state` is typed `string`** and only `'ready'` is ever compared. **`draining` is
  declared but unreachable** — never written anywhere in `app/`, `scripts/` or `tests/`.
- **Refusal reasons are not in the exported OpenAPI contract**, so any frontend union is
  hand-maintained and will drift.
- **`FleetDirectoryService` loads once per session**, `refresh()` has no caller, and a failed cold
  load rejects forever (`Frontend/src/app/.../fleet-directory.service.ts:36-41,59,65-70`) — a boot
  blip disables every execution command for the session. It fails closed, but silently.
- **Seven fleet modules emit no logs whatsoever**, and the enrolment/retirement ceremonies are
  CLI-only. Whether ceremony *evidence* should be addressable is undecided.

### Known frontend defect affecting command safety

The binding-generation fence is **re-read from a live directory signal at click time** on the
panel and roster surfaces (`bot-panel-shell.component.ts:135-143`, `:294`;
`bots-list-page.component.ts:99-108`, `:353`). It is genuinely frozen-at-open only on the Deploy
drawer. Latent today only because the directory loads once per session.

## 8. Divergences from the accepted authorities

Where a document claims something the code does not do. **The code is authority; these are the
claims to fix.**

| Claim | Where | Reality |
|---|---|---|
| Decision 2: nothing opens on the volume before the identity gate | ADR 0062; comment at `app/main.py:383-386` | **Holds.** The profiles-DB writer and the installation lock file now open only after the gate. `fleet_boot.py:159-162`'s registry open still precedes it, but that registry lives on the coordinator's **control** volume, not the clerk's lane volume — the gate has to open the registry it is checking the lane volume against, so this one is structurally unavoidable, not a violation |
| Decision 5: provider safety gates answer before any mutation | ADR 0062 | Dispatched now at the routing seam (§4) — but the Alpaca adapter declares no refusal the seam can currently trigger. The gate is structurally live and semantically empty for the only production provider (owner decision pending) |
| The coordinator "never serves an unscoped agent family itself" | `CONTEXT.md:2121` | It serves two unscoped agent-family reads |
| "The browser secret terminates at the coordinator" | `CONTEXT.md`, composed-auth bullet | Documentation, not code (§4) |
| Retained unscoped **mutations** will retire with the compatibility mechanism | `docs/design/fleet-b-route-inventory.md` | `_SAFE_METHODS = frozenset({"GET", "HEAD"})` (`app/broker/fleet/lane_runtime.py:31`) — the mechanism **can never reach mutations**. Retirement is also all-families-or-nothing despite a per-family constant, and the gate is clerk-agent-only, so it cannot reach the browser's actual compatibility reads |
| The refusal vocabulary has 16 / 21 families | PRD / `app/broker/fleet/errors.py` | The true wire vocabulary is **25** (22 declared + 3 minted inline) |
| `production_adapter()` resolves live adapters | `app/broker/fleet/provider.py:348-350` | Resolves against a deliberately-empty constant, has no callers, always refuses `"alpaca"`. The live registry is `app/broker/fleet_composition.py:27-29` |

### One stranded route

`GET …/order-groups` has **no fleet path**. The frontend calls it unscoped; it is not in the
catalog, not bridged, and mounted clerk-only — so it 404s in fleet posture.

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

### `corpus_coverage` — a stamp on paper, a blocker anywhere else
*(home for [ADR 0054](architecture/adrs/0054-corpus-coverage-is-a-stamp-on-paper.md))*

Whether the golden corpus behind a program's `golden_trace_root` actually covers the symbol **and**
the exact parameter values a bot resolved. The registry qualifies one `validated_settings` point per
program over its `validated_symbols`; any other symbol, or any parameter value differing from that
point, is `UNCOVERED`.

It is a fact about the *evidence a run can later claim*, not about the bytes it runs — artifact and
wiring digests still match their receipt — so since ADR 0054 it no longer refuses the build proof.

Whether an uncovered point may start depends on `account_mode`:

- **Proven paper** (and Dry Run, whose synthetic authority is paper by construction): the run
  starts, and every surface says so. The Start/Resume `explanation` carries *"Corpus coverage is
  UNCOVERED: the paper environment admits this exploratory run, which is not citable as
  qualification evidence"*; the build fact reads `corpus_coverage: UNCOVERED` with a `next_step`
  naming the two routes to a covered run (deploy at the registered validated settings, or run
  golden qualification for these parameters); and the run's frozen evidence keeps the stamp, so the
  panel never replays an exploratory run as citable proof.
- **Live**: the run refuses with `PROGRAM_CORPUS_UNCOVERED`.

A custody answer cannot omit its environment, so there is no third case. **There is no toggle** —
the environment is the only switch, and a paper run that must be corpus-covered simply deploys at
the validated point and reads `COVERED`.

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

## 10. What this document replaced

`docs/broker-v2-operator-manual.md` was generated under
[ADR 0041](architecture/adrs/0041-generated-operator-button-reference.md) — **retired 2026-09-14**,
because this change deletes its entire subject. Its Button Reference and
Glossary were produced from `app/broker/v2panel/vocabulary.py` and CI-gated with
`git diff --exit-code` over the source and served copies.

**What retiring it cost, precisely.** The closed-enum invariant does **not** depend on the manual.
`tests/broker/v2panel/test_vocabulary_snapshot.py` — with committed snapshots on both sides
(`app/broker/v2panel/vocabulary.snapshot.json` and
`Frontend/src/app/components/broker/v2-panel/lib/broker-v2-vocabulary.snapshot.json`) and its own
CI job, which is **not** retired — pins snapshot↔live-set parity, the copy-coverage rule,
`Literal`↔collection parity across all nine vocabularies, and reconciliation-verdict lockstep with
the clerk model.

So enum closure, no-phantom-member, no-undocumented-new-member, copy coverage and frontend/backend
snapshot sync all survive. **What was lost is narrower: the guarantee that the operator-facing
*prose* listing cannot go stale.** That is now a maintenance obligation on this document rather
than a CI gate — recorded here so the loss is visible rather than silent.

## Related

- Runbooks: `docs/runbooks/fleet-dev-two-lane-posture.md`,
  `docs/runbooks/fleet-d-two-clerk-rollout.md`, `docs/runbooks/fleet-d-recovery-and-rollback.md`,
  `docs/runbooks/fleet-e-compatibility-retirement.md`,
  `docs/runbooks/fleet-directory-unavailable.md`
- Moving the installation to another Mac (export, import, go-live, going back):
  `docs/runbooks/migrate-installation.md`
- Alpaca clerk recovery: `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md`
- Route and lane inventories: `docs/design/fleet-b-route-inventory.md`,
  `docs/design/fleet-a2-lane-inventory.md`, `docs/design/fleet-d-runtime-ownership-matrix.md`
- Open defects: `docs/known-gaps.md`
