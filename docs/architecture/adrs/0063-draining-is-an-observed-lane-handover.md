# ADR 0063 — Draining is an observed lane handover, and the release ceremony proves quiet instead of attesting it

**Status:** Accepted 2026-09-15
**Provenance:** Stream C of the fleet lane visibility and handover plan (`docs/superpowers/plans/2026-09-15-fleet-lane-visibility-and-handover.md`), task C1. The defect it closes was found by reading the fleet spine delivered under ADR 0062: `draining` is read by the router (`PythonDataService/app/broker/fleet/service.py:1268-1274`) and by the registry-backup manifest's comment (`PythonDataService/app/broker/fleet/recovery.py:196-199`), admitted by the schema trigger (`PythonDataService/app/broker/fleet/schema.py:285-293`), and **written by nothing** in `app/`, `scripts/` or `tests/`. The owner resolved the lane-exit semantics on 2026-09-15: a lane leaving service stops accepting new routed work, finishes what is already in flight, and hands over — **open positions and running bots stay with the account, and draining never requires the account to be flat.** Moving infrastructure must never force a trading decision.
**Decision drivers:** The release ceremony that `draining` should support gates on `RELEASE_PROOF_TOKEN` (`service.py:109`), a fixed string the operator types to swear "the old agent and volume are offline and the provider's obligations are clear" (`service.py:1052-1058`, `:1138-1144`). The string is `old-clerk-offline-and-obligations-clear`; it is defined in the same source file that checks it, exported in that module's `__all__` (`service.py:1931`), imported by the test suite (`PythonDataService/tests/broker/fleet/test_registry_recovery.py:35`), and **printed verbatim as a copy-pasteable command line in a committed runbook** (`docs/runbooks/fleet-e-registry-recovery-exercise.md:89`). Nothing about typing it is evidence of anything. Under the owner's standing principle — *a check that cannot fire is worse than no check* — it is not a weak gate, it is a gate-shaped hole that makes an unproven release look ceremonious. Meanwhile the registry already holds a durable, append-only, trigger-protected ledger of every command it handed to a lane (`routing_receipts`, `schema.py:218-239`) and has never consulted it.
**Related:** ADR 0062 (the fleet control plane this extends — its Decision 4 "assignment ownership never expires; reassignment is a proof-driven host ceremony", its Decision 8 host-ceremony boundary, and its 2026-09-13 addendum items 1 and 7, which this ADR applies rather than amends), ADR 0047 (authority recovery is an offline ceremony performed against a stopped authority — the precedent for "the proof is captured out of band, from the party that can actually see it"), ADR 0035 / ADR 0037 (the Clerk's SQLite custody authority, unchanged: nothing here moves custody between volumes), ADR 0052 (a terminal exit is operator-declared and re-verified at commit), ADR 0022 (`int64 ms UTC` — every instant introduced below).
**Vocabulary:** `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)" — extended with **drain**, **in-flight attempt**, **coordinator quiet**, **lane quiet** and **mount attestation**. Per ADR 0040 Decision 4.

## Context

### What the code does today

`retire_clerk` (`service.py:435-479`) moves a clerk straight from `provisioned` to `retired` and refuses while the clerk holds any assignment that is not `released` (`service.py:447-461`). The refusal's own `next_step` says "Run the host release ceremony for each assigned account, then retire" — so retirement already delegates its hard question to release. Release is `release_assignment` (`service.py:1034-1111`), and the whole of its proof is the equality test at `service.py:1052`. `reassign_assignment` (`service.py:1114-1222`), the lane-to-lane handover path, repeats the same test at `service.py:1138`. Neither is reachable over HTTP: all three are CLI subcommands of `scripts/manage_broker_fleet.py` (`retire` at `:767`, `release-assignment` at `:772`, `reassign` at `:853`), which is ADR 0062 Decision 8's host-ceremony boundary and stays that way here.

So the intermediate state the whole design implies is missing. `_project_lifecycle` (`service.py:1890-1922`) will faithfully project `draining` to the directory the moment a row carries it (`service.py:1911-1912`), and `resolve_route` will refuse every routed operation for it (`service.py:1268-1274`). The projection and the fence were built. The transition was not.

### What the registry can actually observe

**`routing_receipts` is the coordinator's own obligation ledger, and it is already precise enough.** Its state machine (`records.py:62-75`, `schema.py:224-239`) has four values, and the dispatch timestamp is a *separate* column from the state. `open_routing_attempt` (`service.py:1362-1436`) persists the pinned context as `not_dispatched` before delivery. `mark_routing_dispatched` (`service.py:1437-1453`) then sets `dispatched_at_ms` **and leaves the state alone** — the store's UPDATE touches only `dispatched_at_ms` and `updated_at_ms` (`store.py:932-941`). Only `settle_routing_attempt` (`service.py:1455-1520`) moves the state, and it explicitly refuses to settle back to `not_dispatched` (`service.py:1475-1478`).

That yields one exact, durable predicate:

> An attempt is **in flight** iff `state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL`.

`dispatched_at_ms IS NULL` with the same state is the opposite fact — provably never sent — which `open_routing_attempt`'s docstring already names as the point of writing before dispatch. Both halves are protected by triggers: the pinned context is immutable (`trg_routing_receipts_outcome_only`, `schema.py:361-376`), rows are never deleted (`schema.py:378-382`), a delivered outcome is never downgraded (`schema.py:386-391`), and a dispatched attempt is never un-dispatched (`trg_routing_receipts_dispatch_monotonic`, `schema.py:395-400`). This is the strongest evidence in the fleet registry and the drain ceremony's natural gate.

**`_inflight` is not that evidence, and gating a drain on it would rebuild the defect.** `_CapacityPool._inflight` (`lane_runtime.py:120`, mutated at `:128`, `:141`, `:149`) fails as drain evidence on four independent counts, each sufficient:

1. **It is an in-memory integer in one process.** It is initialised to `0` in `_CapacityPool.__init__` and has no durable backing. A lane restart resets it to zero, so a crashed lane reports perfect quiet.
2. **It does not exist on the coordinator.** `FleetLaneRuntimeMiddleware` builds its pools only when a config is supplied (`lane_runtime.py:460-479`), and its own docstring says it "measure[s] retained reads everywhere and enforce[s] capacity only on an agent" (`lane_runtime.py:450`). The process running the drain ceremony cannot read it.
3. **It counts HTTP calls, not orders.** It is acquired and released around one ASGI request. A market order accepted by the broker and still working contributes `0` — the HTTP response returned long before the order did. The number a drain would most need it to be non-zero for is exactly the number it is zero for.
4. **It is a count, not a set.** It cannot name which obligation is outstanding, so it can never be reconciled — and reconciliation by identity is the entire remedy ADR 0062's addendum item 7 prescribes for an uncertain command.

A drain gated on `_inflight == 0` would pass instantly, always, and prove nothing. That is `RELEASE_PROOF_TOKEN` with a number instead of a string.

**What the coordinator genuinely cannot see.** `routing_receipts` records what *the coordinator routed*. Per ADR 0062 Decision 1 the registry stores no order, fill, position or activation data, and per `docs/design/fleet-d-runtime-ownership-matrix.md:22-23` each lane owns its own "runner state" on its own volume. A bot runner inside a lane that places an order on its own schedule never appears in the coordinator's ledger. So coordinator quiet is a real fact and a **partial** one, and any design that treats it as the whole answer is lying by omission. The lane's own quiet is observable only by the lane.

**And the volume is observable only by the host.** `verify_volume_identity` (`volume.py:201-245`) proves that a root handed to it carries the expected clerk's marker; it runs co-located with its caller and is a statement about *identity*, never about *occupancy*. A mounted-but-idle volume and an unmounted one are indistinguishable from the coordinator. ADR 0062 Decision 8.1 forecloses the only mechanism that would change this — giving the data-plane service container-level authority — because that converts any compromise of the service into a host compromise. The volume fact is irreducibly a host fact.

### The owner decision, checked against the code

**"Positions stay with the account" is fully supported and costs nothing to honour.** Positions live at the broker under the account; the fleet layer's unit of transfer is the `(broker, canonical external account)` assignment, not a position. No existing gate requires flatness — `retire_clerk` checks assignments, not exposure. Draining can be defined without ever asking whether the account is flat, and this ADR does so.

**"Running bots stay with the account and the successor lane adopts them" is *not* supported by the current code, and this ADR does not pretend otherwise.** Runner state lives on the predecessor's volume; a clerk volume is marked once and `write_volume_marker` refuses to overwrite an existing marker outright — "a clerk volume is provisioned once and never re-marked in place" (`volume.py:102-115`). `volume_id` is immutable after provisioning (`trg_clerks_identity_immutable`, `schema.py:263-285`), the active-clerk uniqueness indexes reserve it for any non-retired row (`schema.py:100-107`), and `reassign_assignment` verifies the *successor's own* root before releasing the predecessor (`service.py:1141`). There is no path by which a successor clerk mounts the predecessor's volume, and therefore none by which it inherits a running bot's runner state, ledgers or custody database. What the successor inherits is **the right to write to the account** — after which adopting the account's live positions into its own custody is a provider-owned obligation under ADR 0035 and ADR 0047, and no such adoption ceremony exists in the repo today. Decision 7 records this as an explicit scope boundary rather than burying it.

## Decision

### 1. `draining` becomes a written state with a durable start instant, and entering it is deliberately cheap

A new host ceremony, `drain_clerk`, performs `provisioned -> draining`. Its preconditions are the minimum that keeps the registry coherent:

- the registry recovery hold is clear (`require_recovery_hold_clear`, `recovery.py:400-423`), as every other mutation already requires;
- the clerk exists and is `provisioned`;
- a clerk already `draining` returns its existing record unchanged (Decision 6.3), and a `retired` clerk refuses.

**Nothing else gates it.** Entering a drain is the one transition that must never be blocked, because it is the act that closes the door and makes every later gate converge: `resolve_route` already refuses a draining clerk (`service.py:1268-1274`), so the transition *is* the stop-accepting-new-work step. A drain that could be refused for being inconvenient would leave an operator unable to stop the bleeding, which is the opposite of the ceremony's purpose.

The transition writes **`draining_since_ms`**, a new `int64 ms UTC` column on `clerks`. The table carries only `created_at_ms` and `retired_at_ms` today (`schema.py:93-95`), so without this column the bounded outcome in Decision 4 cannot exist — there is no instant from which to measure. Schema v5, additive `ALTER TABLE ... ADD COLUMN` behind the registered migration path, with the same `>= 0 AND <= MAX_TIMESTAMP_MS` bound every other `*_ms` column carries (`schema.py:48-50`) and a `CHECK` requiring it non-null whenever `lifecycle_state = 'draining'`. It is *not* paired to retirement the way `retired_at_ms` is (`schema.py:95`): a clerk that drained and then retired keeps both, and a clerk that went straight to `retired` without ever serving keeps neither — the direct `provisioned -> retired` edge the trigger already allows (`schema.py:285-293`) stays available for a lane that never served.

The lane learns it is draining from the heartbeat. `observe_session` (`service.py:669-717`) returns a bare `bool` today; it returns the clerk's durable lifecycle state instead. This is a *read* of a fact the registry already holds and grants the lane no new authority — consistent with ADR 0062 addendum item 1, which forbids observations from confirming anything but says nothing against the coordinator telling a lane what the registry has decided about it.

### 2. `draining -> retired` requires three observed facts, and the token is deleted

`RELEASE_PROOF_TOKEN` is removed from `service.py:109`, from `__all__` (`service.py:1931`), from both call sites (`service.py:1052`, `:1138`), from the `--proof` CLI flag, from the test imports, and from the runbook line that prints it (`docs/runbooks/fleet-e-registry-recovery-exercise.md:89`). It is replaced by the following, each of which is named by what observes it.

**Fact A — coordinator quiet (observed by the registry).** Zero routing receipts for this clerk satisfy `state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL`. Computed inside the same transaction as the lifecycle write, so a dispatch racing the ceremony cannot slip between the check and the transition. This needs a supporting partial index on `(clerk_id)` over that predicate; the existing indexes (`schema.py:245-254`) serve the audit read's ordering, not this test.

**Fact B — lane quiet (observed by the lane, *confirmed* to the registry).** A new `confirm_quiescent` call from the lane asserts that it holds no working order and is running no bot decision loop, carrying its own `observed_at_ms`. It is modelled on `confirm_assignment` (`service.py:873-972`), not on the heartbeat, and it inherits that method's fences exactly: it is refused unless the calling `agent_instance_id` and `routing_epoch` equal the current session's (the comparison at `service.py:934-940`), and the comparison happens inside the transaction that records it. It is additionally refused if `observed_at_ms < draining_since_ms` — a quiescence observed before the door closed proves nothing about the period after it.

The distinction between confirmation and heartbeat is the whole point. ADR 0062's addendum item 1 is categorical: *observed facts never confirm anything*, and a heartbeat carrying plausible binding facts leaves routing closed. A `reported_summary` field (`records.py:89-150`) would be an observation and would be worth exactly as little as the token. A confirmation fenced by instance and epoch is the shape this registry already trusts for the one other fact that gates execution.

**Fact C — no unreleased assignment.** Unchanged from `retire_clerk` (`service.py:447-461`). Release or reassign first, then retire. This is already correct and is not re-litigated.

**What survives as an operator attestation, and why exactly one thing does.** The old token bundled three claims. Two of them become Facts A and B. The third — *the old volume is offline* — survives, because the host is the only party that can see its own mounts and ADR 0062 Decision 8.1 deliberately denies the coordinator the authority that would let it look. The **mount attestation** therefore remains an operator input, and it is the only one.

It is not a fixed string. It follows the shape `closeout_empty_registry_recovery` already uses for the one comparable irreversible host act (`recovery.py:426-473`): a bounded `operator` (non-empty, ≤128 chars) and a bounded `change_ref` (non-empty, ≤512 chars), recorded with the attestation instant. This is honest about what it buys: **attribution, not proof.** A change reference is unique per ceremony and names who acted and why, so the audit record can be read afterwards and a second ceremony cannot be performed by replaying the first one's argument. A shared phrase published in the repository is replayable by construction and names nobody. The difference is not strength; it is that one of them is a record and the other is theatre. The load-bearing proof has moved to Facts A and B, which is where it can actually live.

### 3. Routing receipts are the in-flight evidence; `_inflight` is capacity accounting and is never consulted

Settled by the four-count argument in Context. `_inflight` keeps its job — bounding concurrent work on a lane — and acquires no lifecycle meaning. No drain gate reads it, and no future one may: a gate that reads a per-process counter reset by the very crash it is meant to survive is unfalsifiable.

The coordinator's ledger is also declared *partial*, explicitly. Fact A proves the coordinator has no outstanding command. It does not prove the lane has no outstanding work, which is why Fact B exists and is separately owned. A design with only Fact A would be a check that fires but measures the wrong thing, which is the same family of error as one that cannot fire at all.

### 4. A stalled drain has a bounded outcome: wait, then a typed unknown, then a named obligation

A drain stalls when an attempt sits in flight and never settles — the coordinator crashed between `mark_routing_dispatched` and `settle_routing_attempt`, or the lane died holding it. The row stays `not_dispatched` with `dispatched_at_ms` set forever, and nothing in the repo sweeps it.

The bound is an absolute deadline, `draining_since_ms + drain_deadline_ms`, both `int64 ms UTC`, with the duration a deployment-owned setting alongside `DEFAULT_SESSION_STALE_AFTER_MS` (`service.py:100-102`). Behaviour:

- **Before the deadline**, `draining -> retired` refuses while Fact A is false. The drain waits, and the refusal names the outstanding correlation ids so the operator can watch them settle.
- **At or after the deadline**, the ceremony offers one additional move: settle each still-in-flight attempt as `outcome_unknown` through the existing `settle_routing_attempt` path (`service.py:1455`). This fabricates nothing. `outcome_unknown` is defined as "the attempt may have executed and must be reconciled by identity, never resubmitted blindly" (`records.py:62-75`), which is the literally true description of an attempt whose outcome the coordinator lost. The stall is converted from an indefinite wait into a **named reconciliation obligation**, and the retirement record carries every forced correlation id.
- **The deadline never retires anything by itself.** There is no background reaper. A reaper would be a new authority writing outcomes with nobody present, against ADR 0047's established shape for authority-changing acts; and it would make the most consequential step of the ceremony the one nobody watched. The operator re-runs the ceremony and the forced settlements are theirs, attributed by the same `operator` / `change_ref` as the mount attestation.

The second stall mode — the lane never supplies Fact B — is Decision 6.2.

### 5. Drain composes with `reassign_assignment`; it does not replace it, and reassign does not absorb it

The ordering is **drain → (release | reassign) → retire**, and each step keeps its own gate.

`reassign_assignment` (`service.py:1114-1222`) gains one precondition: the **predecessor** clerk must be `draining` and coordinator-quiet (Fact A). That is precisely the claim its `--proof` flag was pretending to make, now observed. Everything else it already does is correct and is retained unchanged — the successor's volume verified before the predecessor's release (`service.py:1141`), the successor required to be currently `provisioned` (`service.py:1157-1163`), the successor required to hold no other active assignment (`service.py:1166-1174`), the generation pinned so evidence cannot cross an ownership change (`service.py:1176-1183`), and the whole release-plus-reserve performed in one transaction (`service.py:1185-1204`).

Fact B is **not** required for reassign, only for retire. This is deliberate: reassignment moves the account's writer, and the earlier that happens after the door closes, the shorter the account spends with no lane able to act on it. Requiring the predecessor's full quiescence confirmation before the successor may even reserve would lengthen exactly the window the ceremony exists to shorten.

The successor is reserved, never confirmed — its own agent must still pass its provider-owned binding and arming gates (`service.py:1116-1118`), and that boot is the seam where the account's live positions must be adopted into the successor's custody. See Decision 7.

`release_assignment` (`service.py:1034`) keeps its role as the terminal path for a lane wound down with no replacement, under the same Fact A + mount attestation gate.

### 6. Failure modes

**6.1 The coordinator is unreachable mid-drain.** Nothing is lost, because nothing about a drain is in memory. `draining_since_ms` is durable, the deadline is an absolute instant rather than a countdown, and Fact A is recomputed from `routing_receipts` on every attempt. The one real consequence is the one Decision 4 handles: an attempt dispatched immediately before the crash stays in flight and correctly blocks retirement until the deadline forces it unknown. That is the design working, not a gap.

One obligation falls out of this. ADR 0062 Decision 7 permits a verified original clerk to recover its already-confirmed last-effective binding while the coordinator is unavailable. **A draining clerk must be excluded from that path**, or a lane whose door the operator closed would reopen it during precisely the window in which nobody can observe it. The FR-066 offline recovery path must read the durable lifecycle state from its confirmation evidence and refuse for a draining lane.

**6.2 The draining lane crashes mid-drain.** Its session goes stale. Per ADR 0062 Decision 4 that transfers nothing and releases nothing — unchanged and not weakened here. Exposure does not widen, because a draining lane is unroutable regardless of its liveness (`service.py:1268-1274`). What is lost is the lane's testimony: Fact B will never arrive.

Retirement is then still possible, on one path and with one record: the in-flight set is force-settled per Decision 4, and the retirement record carries `lane_confirmation: absent` as an explicit recorded fact alongside the mount attestation. **The registry never writes a confirmation the lane did not give, and never treats its silence as assent.** This is the direct application of the owner's standing principle in the other direction: a missing fact is recorded as missing and read as missing, rather than being rounded up to a pass.

Note what is *not* used here: a stale heartbeat is never itself the gate. ADR 0062 Decision 4 forbids liveness from moving ownership, and this ADR does not carve an exception. Staleness corroborates; the confirmation fences. The reason silence is safe in a drain and unsafe in a heartbeat timeout is structural: in a drain the registry had already closed the door before the silence began, so a returning agent cannot be routed to. A heartbeat timeout has no such door.

**6.3 A drain is started against a lane that is already draining.** Idempotent: the existing record returns with `draining_since_ms` **untouched**. This mirrors `open_routing_attempt`'s same-key resume (`service.py:1391-1400`) and `reserve_assignment`'s same-owner resume. Re-draining must not extend the deadline — if it did, a retry loop or an impatient operator could push the bound out indefinitely and Decision 4's bound would be decorative.

Drain is not reversible. The schema trigger already refuses `draining -> provisioned` (`schema.py:287-293`) and stays as written. An operator who drains the wrong lane provisions a new one; the mistaken lane's identity is spent. This matches retirement's terminality and ADR 0062 Decision 3's "IDs are never recycled", and it is accepted rather than worked around — a reversible drain would mean a lane could reopen its door, and every gate downstream would have to re-prove itself against a moving target.

### 7. Scope boundary: the fleet layer hands over the account, not the account's work

Under the owner decision, draining never asks whether the account is flat and never issues a flatten. No gate in this ADR reads a position, and none may be added: the coordinator computes no positions or exposure at all (ADR 0062 Decision 1), and a drain gate that consulted one would drag a trading decision into an infrastructure move.

What a successor lane receives from `reassign_assignment` is the reserved right to write to the account. It does **not** receive the predecessor's custody database, ledgers, receipts or runner state — those are on the predecessor's volume, which is marked once and never re-marked (`volume.py:102-115`) and whose identity is immutable (`schema.py:263-285`). Reconstructing the account's live positions into the successor's own custody is therefore a provider-owned act under ADR 0035 and ADR 0047, performed at the successor's boot, and **no such adoption ceremony exists in the repository today.**

This ADR records that as an open obligation rather than assuming it. Until an Alpaca-side adoption ceremony exists, a reassignment leaves the account's open positions held at the broker with a successor lane whose custody does not yet describe them — which is a safe posture to *pause* in (the successor is reserved, not confirmed, so execution routing stays closed per `service.py:1300-1315`) and an unsafe one to *resume* from. **Reassignment must not be exercised against an account with open positions until that ceremony ships.** The drain ceremony is complete and useful without it: draining, releasing and retiring a lane whose account is flat or whose account is being wound down needs nothing from it.

## Considered and rejected

**Gate the drain on `_inflight == 0`.** Rejected on the four counts in Context, any one of which is fatal. It is the brief's own candidate and it is worse than the token, because a number that is always zero looks like a measurement.

**Let a stale heartbeat complete the drain.** Rejected because ADR 0062 Decision 4 exists for a specific reason — a network partition must never manufacture a second broker writer — and this ADR is not the place to re-open it. Decision 6.2 takes the longer path deliberately.

**A background reaper that force-settles in-flight attempts on a timer and retires the lane.** Rejected on the same grounds ADR 0047 rejected the orchestrated quiesce protocol: it makes the riskiest step of the ceremony the one nobody watched, adds a new authority writing outcomes autonomously, and its only product is saving the operator a second command. The deadline in Decision 4 gives the bound; the operator gives the act.

**Make the drain require the account flat.** Rejected by owner decision, and correctly: it would make every infrastructure move a trading decision, and it would make draining impossible for exactly the lanes most in need of it.

**Keep the token as a secondary confirmation alongside Facts A and B.** Rejected. A gate that adds nothing but is present teaches operators that gates are formalities, and it would give the next reader the impression the ceremony has three proofs when it has two and an attribution.

**Carry Fact B on the existing heartbeat's `reported_summary`.** Rejected: ADR 0062's addendum item 1 settles that an observation confirms nothing, and a quiescence claim that arrives as a heartbeat field would be exactly as strong as the token it replaces.

## Consequences

- **Every gate in the ceremony is now a fact somebody observed**, and each is attributed to the party that can see it: the registry sees its own ledger, the lane sees its own orders, the host sees its own mounts. The one surviving operator input is named as attribution rather than proof.
- **`draining` stops being vocabulary and becomes state.** The router's existing refusal (`service.py:1268-1274`) and the directory's existing projection (`service.py:1911-1912`) become reachable rather than dead branches.
- **Schema v5** adds `draining_since_ms` and a partial index over the in-flight predicate. Both are additive; no table rewrite, no row loss, and the registry stays custody-free.
- **Removing `RELEASE_PROOF_TOKEN` is a breaking change to the host CLI**, its tests (`test_registry_recovery.py:35`, `:486`, `:521`, `:540` and the four `release_assignment` call sites across `tests/broker/fleet/`) and one runbook line (`docs/runbooks/fleet-e-registry-recovery-exercise.md:89`). There is no public API surface to regenerate — all three ceremonies are CLI-only — so the OpenAPI contract and the generated frontend builders are untouched.
- **A drain can now block a retirement indefinitely in one narrow sense**: until its deadline. That is the intended cost. Before this ADR retirement could not be blocked by an in-flight order at all, because nothing looked.
- **Accepted negative:** the ceremony is longer and has more steps, and an operator draining a healthy lane must now wait for real work to finish instead of asserting it already has. That is the point of the change.
- **Accepted negative:** Fact B's predicate — "no working order and no running bot decision loop" — is specified here as a *shape and a fence*, not as a provider implementation. The fleet layer's `authority_state` is a bounded snake-case token with no closed vocabulary at this layer (`records.py:84`, `:139-142`), and nothing in the fleet package can compute the predicate. The Alpaca adapter owes it, and until it ships, Fact B is unavailable and every retirement takes Decision 6.2's `lane_confirmation: absent` path. That is a degraded but honest posture: it records what it knows.
- **Accepted negative and open obligation:** Decision 7's adoption gap. `reassign_assignment` is safe to exercise only against a flat or wound-down account until a provider-owned position-adoption ceremony exists.
- **This ADR records a decision, not a conformance claim** (ADR 0039). At the time of writing nothing writes `draining`, the token is still checked at both call sites, and `confirm_quiescent` does not exist.

## Anti-patterns rejected

- A fixed proof string, a shared passphrase, or any attestation whose expected value is published in the artifact that checks it.
- Gating a lifecycle transition on a per-process in-memory counter that a restart resets.
- Treating a count as evidence where a set is needed — an obligation that cannot be named cannot be reconciled.
- Letting heartbeat silence release, transfer, or retire anything.
- A background process that settles command outcomes or retires lanes with no operator present.
- Recording a lane confirmation the lane did not give, or reading its absence as assent.
- Extending a drain's deadline on a repeated drain request.
- Making a drain reversible.
- Requiring the account to be flat, issuing a flatten as part of a drain, or reading a position in any drain gate.
- Claiming a successor lane inherits the predecessor's custody, ledgers, receipts or runner state — it inherits a volume it never mounted, which is to say nothing.
- Any timestamp in this ceremony that is not `int64 ms UTC`.

## References

- ADR 0062 — the control plane this extends; Decisions 1, 3, 4, 7 and 8 and addendum items 1 and 7 are applied unchanged.
- ADR 0047 — the precedent for out-of-band proof captured by the party that can see it.
- `PythonDataService/app/broker/fleet/service.py` — `retire_clerk` (`:435`), `release_assignment` (`:1034`), `reassign_assignment` (`:1114`), `resolve_route` (`:1224`), the routing-attempt lifecycle (`:1362`, `:1437`, `:1455`), `_project_lifecycle` (`:1890`).
- `PythonDataService/app/broker/fleet/schema.py` — the `clerks` table (`:75-96`), the lifecycle trigger (`:285-293`), `routing_receipts` and its four triggers (`:218-239`, `:361-400`).
- `PythonDataService/app/broker/fleet/lane_runtime.py` — `_CapacityPool` (`:112-151`) and the agent-only enforcement (`:449-479`).
- `PythonDataService/app/broker/fleet/volume.py` — `write_volume_marker` (`:102`) and `verify_volume_identity` (`:201`).
- `PythonDataService/app/broker/fleet/recovery.py` — `require_recovery_hold_clear` (`:400`) and `closeout_empty_registry_recovery` (`:426`), the attestation shape adopted here.
- `docs/design/fleet-d-runtime-ownership-matrix.md` — per-lane ownership of runner state.
- `CONTEXT.md` § "Broker clerk fleet (resolved 2026-09-12)" — domain terminology.
