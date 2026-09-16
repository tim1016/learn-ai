# ADR 0063 — Draining is an observed lane handover, and order-quiet is not observable yet

**Status:** Accepted 2026-09-15
**Provenance:** Stream C of the fleet lane visibility and handover plan (`docs/superpowers/plans/2026-09-15-fleet-lane-visibility-and-handover.md`), task C1, revised after adversarial review returned SPEC FAIL / DESIGN FAIL against the first draft. The defect it closes was found by reading the fleet spine delivered under ADR 0062: `draining` is read by the router (`PythonDataService/app/broker/fleet/service.py:1269-1274`) and referenced by the registry-backup manifest's comment (`PythonDataService/app/broker/fleet/recovery.py:196-199`), admitted by the schema trigger (`PythonDataService/app/broker/fleet/schema.py:285-293`), and **written by nothing** in `app/`, `scripts/` or `tests/`. The owner resolved the lane-exit semantics on 2026-09-15: a lane leaving service stops accepting new routed work, finishes any order already in flight, and hands over — **open positions and running bots stay with the account, and draining never requires the account to be flat.** Moving infrastructure must never force a trading decision.
**Decision drivers:** The release ceremony `draining` should support gates on `RELEASE_PROOF_TOKEN` (`service.py:109`), a fixed string the operator types to swear "the old agent and volume are offline and the provider's obligations are clear" (`service.py:1052-1058`, `:1138-1144`). The string is `old-clerk-offline-and-obligations-clear`; it is defined in the same source file that checks it, exported in that module's `__all__` (`service.py:1931`), imported by the test suite (`tests/broker/fleet/test_registry_recovery.py:35`), and **printed verbatim as a copy-pasteable command line in a committed runbook** (`docs/runbooks/fleet-e-registry-recovery-exercise.md:89`). Under the owner's standing principle — *a check that cannot fire is worse than no check* — it is not a weak gate but a gate-shaped hole that makes an unproven release look ceremonious. The same principle governs the fix, and it is why this ADR's central finding is a negative one: **the fleet layer cannot observe whether an order is working, and no rearrangement of the tables it has will change that.**
**Related:** ADR 0062 (the fleet control plane this extends — Decisions 1, 3, 4, 7, 8 and addendum items 1 and 7 are applied, not amended), ADR 0047 (authority recovery is an offline ceremony against a stopped authority — the precedent for proof captured out of band by the party that can see it), ADR 0035 / ADR 0037 (the Clerk's SQLite custody authority, unchanged: nothing here moves custody between volumes), ADR 0052 (a terminal exit is operator-declared and re-verified at commit), ADR 0039 (Status is decision standing, not code conformance — load-bearing here, because this decision deliberately ships incomplete), ADR 0022 (`int64 ms UTC`).
**Vocabulary:** `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)" — extended with **drain**, **command quiet**, **order quiet**, **lane quiet**, **unsettled attempt** and **mount attestation**. Per ADR 0040 Decision 4.

## Context

### What the code does today

`retire_clerk` (`service.py:435-479`) moves a clerk straight from `provisioned` to `retired` and refuses only while the clerk holds an assignment that is not `released` (`service.py:447-461`). Its refusal text says "Run the host release ceremony for each assigned account, then retire" — so retirement delegates its hard question to release. Release is `release_assignment` (`service.py:1034-1111`), whose entire proof is the string equality at `service.py:1052`. `reassign_assignment` (`service.py:1114-1222`), the lane-to-lane handover, repeats it at `service.py:1138`. None is reachable over HTTP: all three are subcommands of `scripts/manage_broker_fleet.py` (`retire` `:767-770`, `release-assignment` `:772`, `reassign` `:853`), which is ADR 0062 Decision 8's host-ceremony boundary and stays that way.

The intermediate state the design implies is therefore missing. `_project_lifecycle` (`service.py:1890-1922`) will project `draining` to the directory the moment a row carries it (`:1911-1912`), and `resolve_route` will refuse every routed operation for it (`:1269-1274`). The projection and the fence were built. The transition was not.

### The central finding: order-quiet is not observable, by anything in the fleet layer

The brief's binding constraint is that a draining lane **finishes any order already in flight**. Nothing in the repository can tell whether it has.

**The routing ledger settles on the HTTP response, not on the order.** `routing_receipts` (`schema.py:218-239`) looks like an obligation ledger and is not one. `open_routing_attempt` (`service.py:1362-1436`) writes the pinned context as `not_dispatched`; `mark_routing_dispatched` (`service.py:1437-1453`) sets only `dispatched_at_ms`, leaving the state alone (`store.py:932-941`); and `settle_routing_attempt` moves the state to `delivered` **the instant the lane returns any sub-400 response** (`routing.py:485-489`). So the predicate `state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL` is true for exactly one HTTP round trip — or forever, if the coordinator died mid-call. It is never true *because an order is working at the broker*. A market order accepted and still open settles the receipt to `delivered` immediately, exactly as it contributes zero to a capacity counter.

This is the same indictment this ADR levels at `_CapacityPool._inflight` below, and it applies to the receipts ledger verbatim. The first draft of this ADR made that error and is corrected here rather than defended.

**Most orders never enter the ledger at all.** The routed operation catalog holds **76 operations** (`app/broker/fleet/operation_catalog.snapshot.json`), and it is a bot and configuration control surface — `bot_admission_plan`, `bot_authority_facts`, `bot_chart_history` and their siblings. An order placed by a lane's own bot runner on its own schedule never reaches the coordinator, so no receipt exists for it. Per ADR 0062 Decision 1 the registry stores no order, fill or position data by design, and per `docs/design/fleet-d-runtime-ownership-matrix.md:22-23` runner state is lane-local. The coordinator's blindness here is architectural, not an oversight to be patched.

**Routed streams leave no trace either.** `stream_read` (`routing.py:251-320`) resolves and dispatches without ever calling `open_routing_attempt`. A lane serving an open SSE subscription is invisible to the receipts table.

**And the ledger leaks in-flight rows for reasons unrelated to orders.** `deliver_command` calls `mark_routing_dispatched` (`routing.py:422`) and then, at `routing.py:444-445`, `except FleetControlError: raise` — re-raising **without settling**. Any `FleetControlError` from the delivery path leaves a receipt permanently dispatched-and-unsettled, and nothing in the repo sweeps such rows. So the predicate is not merely blind to orders; it accumulates false positives over time.

`_CapacityPool._inflight` (`lane_runtime.py:120`, mutated `:128`, `:141`, `:149`) is no better, on four independent counts, each sufficient: it is an in-memory integer a restart zeroes, so a crashed lane reports perfect quiet; it does not exist on the coordinator, whose middleware enforces capacity "only on an agent" (`lane_runtime.py:450`, guarded at `:460-479`); it counts HTTP calls, so a working order contributes zero; and it is a count rather than a set, so the obligation cannot be named and therefore cannot be reconciled — which is the only remedy ADR 0062's addendum item 7 prescribes.

**Conclusion.** Order-quiet is observable only by the lane, which holds the broker connection and its own custody database, and **no lane-side surface exposes it today.** The fleet layer has no substitute and must not manufacture one. A gate that always passes is the defect this ADR exists to remove; relocating it into a different table would be the same failure wearing a schema.

### What the registry *can* observe

Three things, each genuinely durable, and none of them order-quiet:

- **An unsettled attempt** — `state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL`. Correctly read, this means *the coordinator dispatched a command and lost its outcome*. That is a real reconciliation obligation and worth gating on, but it is bookkeeping about the coordinator's own knowledge, not about the broker.
- **Whether a clerk ever served** — `account_assignment_history` (`schema.py:406-417`) and `clerk_session_history` (`schema.py:145`) are both append-only and never pruned (`store.py:704-745`, `:468-473`). A clerk with no row in either, and no current session, provably never served.
- **Volume and assignment identity** — unchanged from ADR 0062.

### What only the host can observe

`verify_volume_identity` (`volume.py:201-245`) proves that a root handed to it carries the expected clerk's marker. It runs co-located with its caller and is a statement about *identity*, never *occupancy*: a mounted-but-idle volume and an unmounted one are indistinguishable from the coordinator. ADR 0062 Decision 8.1 forecloses the only mechanism that would change this, because giving the data-plane service container-level authority converts any compromise of that service into a host compromise. The volume fact is irreducibly a host fact.

### The owner decision, checked against the code

**"Positions stay with the account; drain never forces a flat" is fully supported and costs nothing.** No existing gate reads exposure; the coordinator computes no positions by design. This ADR adds no gate that does.

**"Running bots stay with the account and the successor lane adopts them" is not supported today**, and Decision 8 records that rather than designing around it. Runner state is on the predecessor's volume; `write_volume_marker` refuses to overwrite an existing marker — "a clerk volume is provisioned once and never re-marked in place" (`volume.py:102-115`); `volume_id` is immutable (`trg_clerks_identity_immutable`, `schema.py:263-277`); the active-clerk unique indexes reserve it for any non-retired row (`schema.py:100-107`); and `reassign_assignment` verifies the **successor's own** root before releasing the predecessor (`service.py:1141`). No successor can mount a predecessor's volume, so none inherits runner state, ledgers or custody.

## Decision

### 1. `draining` becomes a written state with a durable start instant, and entering it is deliberately cheap

A host ceremony `drain_clerk` performs `provisioned -> draining`, requiring only that the registry recovery hold is clear (`recovery.py:400-423`) and the clerk is `provisioned`. **Nothing else gates entry.** `resolve_route` already refuses a draining clerk (`service.py:1269-1274`), so the transition *is* the stop-accepting-new-work step; a drain that could be refused for being inconvenient would leave an operator unable to close the door.

It writes **`draining_since_ms`**, a new `int64 ms UTC` column on `clerks` — the table carries only `created_at_ms` and `retired_at_ms` today (`schema.py:93-95`), so without it the bound in Decision 5 has no origin. Schema v5, additive, with the standard `>= 0 AND <= MAX_TIMESTAMP_MS` bound (`schema.py:48-50`) and a `CHECK` requiring it non-null when `lifecycle_state = 'draining'`. It is not paired to retirement the way `retired_at_ms` is: a clerk that drained then retired keeps both.

The lane learns it is draining from the heartbeat. `observe_session` (`service.py:669-717`) returns a bare `bool`; it returns the clerk's durable lifecycle state instead. This is a read of a fact the registry already holds and grants the lane no authority, consistent with ADR 0062 addendum item 1.

**On learning it is draining, the lane is obliged to stop admitting new bot decisions and to wind down its runner.** Nothing in the current code obliges it — `observe_session` refuses only for `RETIRED` (`service.py:688-693`) — and this ADR does not add a coordinator-side enforcement, because the coordinator cannot see a runner. It is a lane-side obligation owed by each provider adapter, and it is stated here so it is designed rather than discovered.

### 2. Order-quiet is a required gate that cannot be implemented yet, and the ceremony ships saying so

`draining -> retired` requires **lane quiet**: the lane holds no working order and runs no bot decision loop, asserted by the lane about its own broker connection and custody.

It arrives as a **confirmation**, not an observation, modelled on `confirm_assignment` (`service.py:873-972`) and inheriting its fences exactly: refused unless the calling `agent_instance_id` and `routing_epoch` equal the current session's (`service.py:934-940`), compared inside the recording transaction, and additionally refused if `observed_at_ms < draining_since_ms` — quiescence observed before the door closed proves nothing about the period after it. A `reported_summary` heartbeat field (`records.py:90-150`) would be an observation and, under ADR 0062 addendum item 1, would confirm nothing; it would be worth exactly as much as the token.

**No provider can currently answer it.** `authority_state` is a bounded snake-case token with no closed vocabulary at the fleet layer (`records.py:86`, `:139-142`), and nothing in `app/broker/fleet/` can compute the predicate. The Alpaca adapter owes: a lane-local query over its own custody database and broker connection returning *no working order and no admitting bot runner*, as one bounded fact with its observation instant.

**Until that ships, `draining -> retired` does not complete.** There is no fallback, no degraded automatic path, and no operator attestation that substitutes. A drain may be entered, a lane may be released or reassigned, and retirement waits. This is the deliberate choice: an operator blocked at a gate that cannot yet be satisfied learns the truth immediately, whereas a gate that degrades to an attestation would reproduce `RELEASE_PROOF_TOKEN` in a new location and teach everyone the ceremony is a formality. The first draft of this ADR contained exactly that unconditioned degraded path; it is removed.

The one relief is Decision 5's `force-retire`, which is a **separate, named, operator-attributed ceremony** — never a fallback inside `retire`.

### 3. Command quiet is a real but narrow gate, and it is not the order gate

`draining -> retired` additionally requires zero unsettled attempts for the clerk (`state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL`), plus the pre-existing no-unreleased-assignment check (`service.py:447-461`).

This is retained at its true strength and no higher: **it prevents retiring a lane past a command whose outcome the coordinator lost.** On a healthy lane it is satisfied immediately and blocks nothing, which is correct — it is not measuring orders and never claimed to. It needs a supporting partial index on `(clerk_id)` over that predicate; the existing indexes (`schema.py:245-254`) serve the audit read's ordering.

Two honest limits ride with it. The routing.py:444 leak means it can be false for reasons having nothing to do with orders, so the leak is a prerequisite fix, not a nice-to-have: the `except FleetControlError` branch must settle `outcome_unknown` before re-raising, as its two sibling handlers do. And routed SSE streams open no receipt at all (`routing.py:251-320`), so an open subscription is invisible to it — either streams gain an attempt row, or the gate's scope is documented as commands only. This ADR requires the former if streams are ever to be part of drain evidence, and accepts the latter meanwhile.

**Claim withdrawn from the first draft:** that computing this inside the lifecycle transaction closes the race. It does not, on its own — `open_routing_attempt` (`service.py:1362-1436`) performs no lifecycle check, and the ceremony runs in a separate host process from the coordinator serving traffic. But the lifecycle *is* checked immediately before it: `_resolve` (`routing.py:500-528`) calls `resolve_route` before `open_routing_attempt` on every command path, and `resolve_route` already refuses a draining clerk (`service.py:1269-1274`). What remains is a narrow time-of-check/time-of-use window between resolve and dispatch — not an unchecked lifecycle — and the pinned `routing_epoch` and `agent_instance_id` `open_routing_attempt` already records (`routing.py:387-402`) fence a session change across that window even without a second lifecycle read.

### 4. What replaces `RELEASE_PROOF_TOKEN`, and the one attestation that survives

The token is deleted from `service.py:109`, `__all__` (`:1931`), both call sites (`:1052`, `:1138`), the `--proof` flag, the tests and the runbook line.

`RELEASE_PROOF_TOKEN`'s own docstring (`service.py:102-109`) bundles **four** claims, not three: the old agent is offline, the old volume is offline, credentials are isolated, and the provider's obligations are clear. Naming a home for each, honestly, rather than rounding the count down and leaving the mapping implicit:

- **The old volume is offline** survives as the operator's mount attestation below — the host alone can observe its own mounts, and ADR 0062 Decision 8.1 deliberately denies the coordinator the authority to look.
- **The provider's obligations are clear** is a claim about the broker, so its only honest home is Decision 2's lane quiet — the one gate that speaks to the broker connection at all. It is **not** Decision 3: command quiet proves only that the coordinator lost no dispatch outcome, and this ADR's own central finding is that this says nothing about the broker. Decision 2 is unimplemented today, so no proof of this claim lives anywhere yet — it is homed, not satisfied.
- **The old agent is offline** and **credentials are isolated** are both unhomed. Nothing in this ADR observes process liveness — Decision 5 forbids heartbeat staleness from gating anything, on ADR 0062 Decision 4's grounds — and nothing here inherits the credentials claim; it is a host fact exactly like the volume claim, but the attestation below names only the volume. Naming these two as unhomed is the finding this accounting owes; dropping them silently would be the token's own failure repeated in a new document.

It is recorded in the shape `closeout_empty_registry_recovery` already uses for the one comparable irreversible host act (`recovery.py:426-473`): bounded `operator` (≤128) and `change_ref` (≤512), non-empty, with the attestation instant. This buys **attribution, not proof** — a change reference is unique per ceremony and names who acted and why; a phrase published in the repository that checks it is replayable and names nobody. **Both `release_assignment` and `reassign_assignment` record their own attestation independently**, exactly as each independently checked the deleted token at its own call site (`:1052`, `:1138`) — one `operator`/`change_ref` per ceremony invocation, not one per account or per clerk.

**Both `release_assignment` and `reassign_assignment` require the clerk to be `draining`, command-quiet, and past `draining_since_ms + drain_deadline_ms`** — the same deadline Decision 5 gates `force-retire` on. The first draft gave the draining/command-quiet precondition to reassign only, leaving release able to consume the gate without the closed door that makes it meaningful; they are symmetric now. Neither draft gave either ceremony a wait tied to order-quiet's own bound — §4.1 states why that gap is a real hazard and closes it this way rather than leaving it open.

### 4.1 The two-writer hazard on release and reassign, named explicitly

Handover is strictly more dangerous than retirement: retirement only stops writes, handover starts a new writer while the predecessor's order-quiet was never established. §7.1 names exactly this hazard for the FR-066 restart case — a predecessor's lane-local runner still placing orders while a second party gains write rights to the same account — and until this revision it went unnamed on the path that most resembles it.

`release_assignment` and `reassign_assignment` proceed on door-closed (`draining`) plus command-quiet plus the mount attestation, with **zero evidence about the predecessor's working orders** — Decision 2's lane quiet is unimplemented, exactly as it is for retirement. Unlike retirement's normal path, the preceding draft's release/reassign preconditions carried no time cost at all: a lane could be drained and reassigned in the same breath, with no wait analogous to `force-retire`'s deadline.

This revision applies the ADR's own proof to handover rather than special-casing it around the gate that makes retirement honest: release and reassignment now require `draining_since_ms + drain_deadline_ms` to have actually elapsed, exactly as `force-retire` requires, and each records `lane_confirmation: absent` as a durable fact on the transition, so an auditor can count how many handovers proceeded without it — today, all of them. This does **not** block release outright: `retire_clerk` refuses while assignments are unreleased, so an unconditional block on release would block every exit from the fleet. It bounds the wait instead, on the same deadline this ADR already uses for the symmetric retirement problem.

**Open hole, not closed by this revision:** the deadline bounds the wait but proves nothing about the predecessor's orders during it, exactly as it proves nothing for `force-retire`. The successor-side mitigation — the successor's runner refusing to admit decisions until it has independent evidence the predecessor is quiet — does not exist, has no owner, and is not among the prerequisite fixes in Consequences. Blast radius: a reassignment that proceeds at or after the deadline against a predecessor whose bot runner never actually wound down (Decision 1 states that obligation but does not enforce it) hands write rights to a successor while the predecessor may still be working an order — two writers against one account. Decision 1's lane-side wind-down obligation is the intended closure and remains unenforced; this hole persists until it, or an equivalent successor-side check, ships.

### 5. A stalled drain is bounded by a deadline and discharged by a named ceremony, never by a fallback

A drain stalls when an unsettled attempt never settles, or when the lane never confirms quiescence because it died.

The bound is an absolute deadline, `draining_since_ms + drain_deadline_ms`, both `int64 ms UTC`, the duration deployment-owned. Before it, retirement refuses and names what is outstanding. At or after it, one additional **separately named ceremony**, `force-retire`, becomes available, and it:

- requires the deadline to have actually elapsed;
- settles each still-unsettled attempt as `outcome_unknown` via `settle_routing_attempt` (`service.py:1455`), which fabricates nothing — that value *means* "may have executed, reconcile by identity, never resubmit" (`records.py:62-75`);
- records, as first-class durable facts on the retirement, `lane_confirmation: absent` and every forced correlation id;
- carries its own `operator` / `change_ref`.

It is a distinct CLI verb, not a branch inside `retire`, so that an operator who force-retires knows they did and an auditor can count how often it happens. **There is no background reaper**: the deadline gives the bound, the operator gives the act, on ADR 0047's grounds.

**`drain_deadline_ms` has a floor: not less than 24 hours (`86_400_000` ms).** The number must be justified against a plausible working-order lifetime, not against `DEFAULT_SESSION_STALE_AFTER_MS` (`service.py:100`) — that 30-second module constant measures heartbeat-staleness detection latency, an entirely different quantity, by five orders of magnitude. A regular-hours DAY order still open at drain time is force-expired by the exchange no later than that session's close; a GTC order can legitimately rest unfilled indefinitely and is a position concern, not an in-flight-order one (Context: drain never requires the account flat). Twenty-four hours spans a full regular session plus its close regardless of when in the session the drain was entered, so a lane that is actually winding down — not merely retrying a dropped heartbeat — has a full trading day to reach lane quiet before an operator is offered `force-retire`'s escape hatch. A deployment may set the value higher; this is a floor, not a default, and a deployment setting it at or near the floor should expect `force-retire` to fire on any lane whose in-flight orders routinely outlast one session.

A stale heartbeat is never itself a gate — ADR 0062 Decision 4 forbids liveness from moving ownership, and no exception is carved. Staleness may corroborate an operator-initiated `force-retire`; it may not trigger one.

**Open gap, named rather than closed:** the forced-unknown correlation ids are recorded against a clerk that is then retired, and Decision 8 establishes that no successor can describe them. They therefore have no owner and no work queue. `force-retire` must emit them to a durable host-side reconciliation record outside the retired clerk's row; this ADR does not specify its home, and leaving them only on the retired clerk would be a silent drop.

### 6. The `provisioned -> retired` bypass is closed against an observable

The trigger allows a direct `provisioned -> retired` edge for "a clerk that never served" (`schema.py:285-293`), but nothing defined or enforced "never served", and `retire_clerk` gates only on unreleased assignments while the CLI passes `--clerk-id` straight through (`scripts/manage_broker_fleet.py:767-770`). So `release-assignment` followed by `retire` skips the drain entirely — and `retire_clerk`'s own refusal text signposts that route.

"Never served" is made an enforced predicate, and it is genuinely observable because both history tables are append-only and never pruned: **no row in `account_assignment_history` for this clerk, no row in `clerk_session_history`, and no current session.** `retire_clerk` evaluates it in the transaction that performs the transition. A clerk that meets it retires directly. **A clerk that has ever served must pass through `draining`**, and `retire_clerk` refuses otherwise.

`retire_clerk`'s refusal text is re-authored to name the drain as the first step, not release. A ceremony nobody is obliged to run fails the same way a check that cannot fire does.

**`update_clerk_lifecycle` (`store.py:414-427`) has a second live caller**, and enforcing the predicate only in `retire_clerk` leaves it standing on a single-call-site convention while the schema trigger (`schema.py:285-293`) still permits `provisioned -> retired` unconditionally. The second caller is the provisioning-compensation path (`service.py:361-367`): when `write_volume_marker` fails after the registry row is committed, the half-born clerk is retired directly, with no predicate call. That specific write is legitimate today — a clerk whose marker write failed provably never served — but its legitimacy is an accident of who happens to call it, not a property the schema enforces. A future caller added to either path would silently inherit the bypass this section exists to close.

The repo's own pattern for exactly this shape is a trigger backstop behind a service pre-check — `schema.py:298-299` says so in as many words, for the volume-nesting fence: "the service's pre-check produces the typed refusal; this is the backstop a race cannot step over." That pattern is adopted here rather than left as a convention this ADR merely hopes every future caller honors: `trg_clerks_lifecycle_forward` (`schema.py:287-295`) gains a fourth `WHEN` arm that aborts a `provisioned -> retired` transition unless the clerk mirrors the enforced predicate exactly — no row in `account_assignment_history`, no row in `clerk_session_history`, and no row in `clerk_sessions` for `OLD.clerk_id` (three `NOT EXISTS` clauses, not two: `clerk_sessions` is an upsert keyed on `clerk_id`, and `archive_session` only populates `clerk_session_history` when a *second* registration supersedes the first, so a clerk that registered exactly once has a live `clerk_sessions` row with no `clerk_session_history` row at all — the history-only check alone would misclassify it as never-served). `retire_clerk`'s check stays — it produces the typed, actionable refusal the trigger's `RAISE(ABORT, ...)` cannot — but the trigger is now the thing a race, or a future caller nobody thought to gate, cannot step over.

This backstop needs a read path the schema does not currently have: **`account_assignment_history`'s primary key is `(broker, canonical_external_account_id, assignment_generation, state)`** (`schema.py:406-417`), with no `clerk_id` column in any key or index, and its only reader, `list_assignment_history` (`store.py:740-750`), is account-scoped. Both the enforced predicate's read in `retire_clerk` and the new trigger's per-row lookup need a clerk-scoped read this table cannot serve today. A supporting index on `account_assignment_history(clerk_id)` is required alongside the partial index Decision 3 specifies for `routing_receipts`; `clerk_session_history` needs no equivalent addition, since `clerk_id` already leads its primary key (`schema.py:145-153`).

### 7. Failure modes

**7.1 Coordinator unreachable mid-drain.** Nothing is lost: no part of a drain is in memory, `draining_since_ms` is durable, the deadline is an absolute instant rather than a countdown, and every gate is recomputed per attempt.

**But ADR 0062 Decision 7's offline recovery path is a hole this ADR cannot close, and its blast radius is stated rather than hidden.** That path lets a verified original clerk recover its already-confirmed last-effective binding while the coordinator is unavailable. The first draft proposed that the path "read the durable lifecycle state from its confirmation evidence"; **it cannot.** `ConfirmationEvidence` (`confirmation.py:48-64`) carries eleven fields and no lifecycle state, is written at confirmation time — necessarily before any drain — and is never re-authored. A lane that confirmed while provisioned and later drained is byte-indistinguishable from a healthy one.

A partial mechanism *could* exist: since Decision 1 has the heartbeat return the lifecycle state, a lane that learns it is draining could re-author its evidence with a drain marker, and the offline path could then refuse. **It does not exist today.** It is not implemented, it is not among the prerequisite fixes this ADR lists in Consequences, and it has no owner. Describing the hole as though that mechanism were already narrowing it — present only for the case where a drain was issued against an already-unreachable lane — overstates what ships with this revision.

**Blast radius, stated plainly, in the present tense:** today, `fleet_boot.py:284-300` enters the offline path on *any* `FleetPresenceError` from `presence.register`, then boots the last-effective binding from the evidence file alone via `offline_boot_matches` (`fleet_boot.py:385-404`); and `register_agent_session` (`service.py:571-572`) refuses only `RETIRED`, never `DRAINING`. Nothing here distinguishes a lane drained *before* an outage from one drained *during* one — because the re-authoring mechanism that would draw that distinction does not exist, every drained lane that restarts while the coordinator is unreachable takes the offline path indistinguishably from a healthy one, reopens its binding via FR-066, and may execute against an account the operator had closed its door on. The window is bounded by coordinator unavailability, but the population it applies to is **any drained lane**, not only one drained while the coordinator happened to already be unreachable. It is not closed by this ADR (tracked as #2155). Closing it requires either a lifecycle field in the confirmation evidence written at drain time by a path that does not depend on the lane being reachable, or the offline recovery path consulting something other than the evidence file. **Reassignment must not be performed against any drained lane until this is closed**, because restart-during-outage is not a distinguishable subset today — it is the general case.

**7.2 The draining lane crashes mid-drain.** Its session goes stale; per ADR 0062 Decision 4 that transfers and releases nothing, unchanged. Exposure does not widen — a draining lane is unroutable regardless of liveness — but its testimony is lost, so Decision 2's gate cannot be satisfied and Decision 5's `force-retire` is the only exit. The registry never writes a confirmation the lane did not give, and never reads silence as assent.

**7.3 A drain against an already-draining lane.** Idempotent: the existing record returns with `draining_since_ms` untouched, mirroring `open_routing_attempt`'s same-key resume (`service.py:1391-1400`) and `reserve_assignment`'s same-owner resume. Re-draining must not extend the deadline, or a retry loop makes the bound decorative.

Drain is irreversible; the trigger already refuses `draining -> provisioned` (`schema.py:287-293`). An operator who drains the wrong lane provisions a new one. This matches retirement's terminality and ADR 0062 Decision 3's non-recycled identities, and is accepted rather than worked around.

### 8. Scope boundary: the fleet layer hands over the account, not the account's work

No gate in this ADR reads a position, and none may be added.

A successor receives the reserved right to write to the account — not the predecessor's custody, ledgers, receipts or runner state, for the reasons in Context. Reconstructing the account's live positions into the successor's custody is provider-owned under ADR 0035/0047, and **no such adoption ceremony exists.** Until it does, a reassignment leaves open positions at the broker with a successor whose custody does not describe them: safe to pause in (the successor is reserved, not confirmed, so execution routing stays closed per `service.py:1300-1315`) and unsafe to resume from. **Reassignment must not be exercised against an account with open positions until that ceremony ships.** Drain, release and retire are useful without it.

## Considered and rejected

**Gate the drain on routing receipts as an order ledger.** This was the first draft's central decision and it is withdrawn. Receipts settle on the HTTP response (`routing.py:485-489`), most orders never enter them, streams never enter them, and the table leaks (`routing.py:444`). It would have been a gate that always passes.

**Gate on `_inflight == 0`.** Rejected on the four counts in Context.

**Keep an unconditioned degraded path so retirement always has an exit.** Rejected: an always-available degraded path makes the gate above it optional structurally, not merely until an adapter ships. Decision 5's `force-retire` is deadline-conditioned, separately named and attributed.

**Let a stale heartbeat complete the drain.** Rejected on ADR 0062 Decision 4's grounds; a partition must never manufacture a second writer.

**A background reaper.** Rejected on ADR 0047's grounds: it makes the riskiest step the one nobody watched, for the sake of saving one command.

**Require the account flat.** Rejected by owner decision, and correctly — it would make every infrastructure move a trading decision.

**Keep the token alongside the new gates.** Rejected: a gate that adds nothing but is present teaches operators that gates are formalities.

**Ship the ceremony with order-quiet unstated, gated on what is observable today.** Rejected as the worst option available. It would produce a ceremony that looks complete, passes on every healthy lane, and silently fails the one constraint the owner made binding.

## Consequences

- **The ceremony is honest about ordering, and incomplete on purpose — and it does not ship "blocked."** Drain entry, the bypass closure and its trigger backstop, command quiet, the release/reassign preconditions (including the deadline wait, §4.1) and `force-retire` are implementable now. `draining -> retired` on the normal, unforced path is blocked until a provider can answer lane quiet (tracked as #2154, which is the named end date for this state). **But that is not the path that ships for any served clerk.** Since no provider can answer lane quiet today, `force-retire` is not an exception — it is the only path to retirement for a served clerk, 100% of the time, and the auditable count of forced retirements starts at 100% and stays there until #2154 closes. An operator retiring a served clerk today runs an attested, deadline-delayed, audited `force-retire`, not the unforced path; describing the ceremony as shipping "blocked" without saying that is the document failing its own standard.
- **`draining` stops being vocabulary**; the router's refusal (`service.py:1269-1274`) and the directory's projection (`:1911-1912`) become reachable.
- **Prerequisite fixes this ADR depends on**, each a bug in its own right and each filed for closure: the `routing.py:444-445` unsettled-receipt leak (#2152); and, if streams are to count, receipts for `stream_read` (#2153). `open_routing_attempt` performing no lifecycle re-check is **not** carried forward as a prerequisite: `_resolve` (`routing.py:500-528`) calls `resolve_route`, which already refuses a draining clerk, immediately before every `open_routing_attempt` call on every command path; what remains is a resolve→dispatch time-of-check/time-of-use window already fenced by the pinned `routing_epoch` and `agent_instance_id`, not an unchecked lifecycle, and it is not filed.
- **Schema v5** adds `draining_since_ms` and a partial index over the unsettled-attempt predicate. Additive; the registry stays custody-free.
- **Breaking change inventory**, verified by grep: `app/broker/fleet/service.py`, `app/broker/fleet/store.py`, `scripts/manage_broker_fleet.py`, and five test modules (`tests/broker/fleet/test_assignments.py`, `test_directory_and_aggregation.py`, `test_provisioning_and_volume.py`, `test_registry_recovery.py`, `test_reviewer_fences.py`, `test_secret_absence.py`) — eight files — plus `docs/runbooks/fleet-e-registry-recovery-exercise.md:89`. **No OpenAPI or frontend regeneration**: all three ceremonies are CLI-only, with no HTTP surface in `app/routers/broker_clerks.py` or `internal_fleet.py`. `observe_session`'s return-type change does alter the body of `/internal/fleet/sessions/observe`, which is an internal agent↔coordinator route: no public contract impact, but it is a **rolling-upgrade consideration** — a coordinator returning the richer body must not break an agent build that expects a bare boolean, so the field is additive and the agent tolerant.
- **Accepted negative:** retirement gets slower, and until a provider answers lane quiet (#2154), every retirement of a served clerk is a `force-retire` rather than the normal path completing on its own — the auditable count starts at 100%. That is the honest consequence of the gate not existing yet, and it is preferred to a gate that passes.
- **Accepted negative and open holes**, each named above with its blast radius: the FR-066 offline-recovery indistinguishability, now stated for any drained lane restarting during an outage rather than only one drained while already unreachable (7.1, #2155); the two-writer hazard on release and reassignment during the deadline wait, with no successor-side mitigation (4.1); the ownerless forced-unknown obligations (5); the streams gap (3, closed by #2153 if streams are ever required as drain evidence); and the position-adoption gap (8).
- **This ADR records a decision, not a conformance claim** (ADR 0039). Nothing writes `draining`, the token is still checked at both call sites, `confirm_quiescent` does not exist, and no provider can answer lane quiet.

## Anti-patterns rejected

- A fixed proof string, or any attestation whose expected value is published in the artifact that checks it.
- Presenting a gate as measuring orders when it measures HTTP round trips, outcome bookkeeping, or process-local capacity.
- An unconditioned degraded path beneath a gate, which makes the gate optional.
- Gating a lifecycle transition on a per-process in-memory counter a restart resets.
- Treating a count as evidence where a set is needed.
- Letting heartbeat silence release, transfer or retire anything.
- A background process that settles command outcomes or retires lanes unattended.
- Recording a lane confirmation the lane did not give, or reading its absence as assent.
- Leaving a ceremony optional by preserving an unenforced direct path around it.
- Extending a drain's deadline on a repeated drain request; making a drain reversible.
- Requiring the account flat, or reading a position in any drain gate.
- Claiming a successor inherits custody, ledgers, receipts or runner state from a volume it never mounted.
- Any timestamp here that is not `int64 ms UTC`.

## References

- ADR 0062 — the control plane this extends; Decisions 1, 3, 4, 7, 8 and addendum items 1 and 7 applied unchanged.
- ADR 0047 — out-of-band proof captured by the party that can see it.
- ADR 0039 — Status is decision standing, not code conformance; load-bearing for a decision that ships incomplete.
- `PythonDataService/app/broker/fleet/service.py` — `retire_clerk` (`:435`), the provisioning-compensation retirement (`:361-367`), `register_agent_session` (`:527`, the `RETIRED`-only refusal at `:571-572`), `observe_session` (`:669`), `confirm_assignment` (`:873`), `release_assignment` (`:1034`), `reassign_assignment` (`:1114`), `resolve_route` (`:1224`), the attempt lifecycle (`:1362`, `:1437`, `:1455`), `_project_lifecycle` (`:1890`), `RELEASE_PROOF_TOKEN`'s four-claim docstring (`:102-109`).
- `PythonDataService/app/broker/fleet/routing.py` — `_resolve` (`:500-528`), `stream_read` (`:251`), `deliver_command` (`:327`), `mark_routing_dispatched` (`:422`), the pinned `open_routing_attempt` call (`:387-402`), the unsettled re-raise (`:444`), the delivered settle (`:485`).
- `PythonDataService/app/broker/fleet/store.py` — `update_clerk_lifecycle` (`:414-427`), `list_assignment_history` (`:740-750`).
- `PythonDataService/app/broker/fleet/schema.py` — `clerks` (`:75-96`), `clerk_session_history` (`:145-153`), `routing_receipts` (`:218-239`), `trg_clerks_identity_immutable` (`:263-277`), the lifecycle trigger (`:285-293`, the trigger body through `:295`), the volume-nesting backstop comment (`:298-299`), the receipt triggers (`:361-400`), `account_assignment_history` (`:406-417`).
- `PythonDataService/app/broker/fleet/confirmation.py` — `ConfirmationEvidence` (`:48-64`), the eleven fields that cannot express a drain.
- `PythonDataService/app/broker/fleet/lane_runtime.py` — `_CapacityPool` (`:112-151`), agent-only enforcement (`:449-479`).
- `PythonDataService/app/broker/fleet/volume.py` — `write_volume_marker` (`:102`), `verify_volume_identity` (`:201`).
- `PythonDataService/app/broker/fleet/recovery.py` — `require_recovery_hold_clear` (`:400`), `closeout_empty_registry_recovery` (`:426`).
- `PythonDataService/app/broker/alpaca/clerk/fleet_boot.py` — the offline-path entry on any `FleetPresenceError` (`:284-300`), `offline_boot_matches` (`:385-404`).
- `PythonDataService/app/broker/fleet/operation_catalog.snapshot.json` — the 76 routed operations.
- `docs/design/fleet-d-runtime-ownership-matrix.md` — per-lane ownership of runner state.
- `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)".
- Issues #2152, #2153, #2154, #2155 — tracked prerequisites and open holes, cited where each applies above.
