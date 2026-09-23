# C4: the two-database confirmation handover (clerk evidence ↔ fleet registry)

Research ticket #2287 on map #2276. Static trace at `a14f1df1` (master, 2026-09-23),
plus one throwaway in-process probe (described under D-1). Every `file:line` is at
that SHA. Paths are relative to `PythonDataService/` unless stated.

## Answer

The handover itself is sound. Whichever side a process dies on, a later restart ends
in a safe state. The registry is the only side that can open routing, and it opens
execution routing only to the session that confirmed (`service._descriptor`,
`app/broker/fleet/service.py:2626-2642`). The clerk evidence can only move the lane
toward "closed". So in the ordinary crash protocol **the registry wins**, and the
evidence matters only for the FR-066 offline boot.

**The registry never routes to a lane that did not confirm.** The one exception is an
*offline-booted* lane. Its identity fence is not installed, so a coordinator forward
that still pins the dead session's epoch reaches the handler unchecked (G-5).

**A lane can act on a grant the registry does not have.** It happens wherever the
lane misreads the registry's answer, not at a crash point:

- **D-1 (proven, P1, filed #2320).** A retired clerk's registration is refused as
  `404 clerk_not_found`. `RemotePresence` classes that refusal as
  "coordinator unavailable". The lane then boots its last-effective binding offline,
  against a coordinator that is **up** and has just told it the lane "never returns
  to service". A throwaway probe reproduced this end to end: force-retire, then
  restart, and the lane comes up `online=False` with `offline_boot_matches == True`.
  After a force-retire the account is released and a successor may take it.
- **D-2 (proven, P1, filed #2321).** An offline-booted lane never tries to register
  again. It starts no heartbeat, so it never rejoins and never learns a drain or a
  retirement, even after the coordinator comes back. It keeps custody, the
  reconciliation sweep and the stuck-EXIT watchdog running, while the directory shows
  it `unreachable`. This moves ADR 0063's "residual window" from "restarted *during*
  an outage" to "**booted** during any outage, however short".

The schema-v2 `lifecycle_state` (#2155) does what it claims when the lane learns its
drain. It has one lost-update race (G-2, where a confirmation overwrites the
tombstone). It also has one path where learning kills the heartbeat instead of
leaving the lesson retryable (D-3, P2).

## (a) Trace

### Who writes what

| Store | Written by | Content |
|---|---|---|
| **Registry: session row** | `register_agent_session` (`service.py:1164-1311`) | instance id, routing epoch (+1 per new instance, `:1288`), last_seen |
| **Registry: assignment row** | `reserve_assignment` (`:1385`), then `confirm_assignment` (`:1677-1848`) | state RESERVED→EFFECTIVE; `confirmed_{binding_generation, profile, revision, agent_instance_id, routing_epoch}` (`:1804-1815`), one `BEGIN IMMEDIATE` transaction fenced by current session + lifecycle (`:1729-1760`) |
| **Registry: clerk row** | `drain_clerk` (`:505`), `retire_clerk` (`:572`), `force_retire_clerk` (`:653`) | lifecycle provisioned→draining→retired. Force-retire also releases held assignments (`:718-748`) |
| **Clerk volume: `fleet/confirmation.json`** | `write_confirmation_evidence` (`app/broker/fleet/confirmation.py:201-250`): tmp + fsync + `os.replace` + dir fsync | the grant confirmed, plus `lifecycle_state` (v2). Always written `provisioned` (`:97`, default). Re-authored to `draining` only by `mark_confirmation_evidence_draining` (`:253-273`) |
| **Clerk process memory** | `confirm_binding` (`app/broker/alpaca/clerk/fleet_boot.py:573-579`), `_learn_drain` (`:697-723`) | `confirmed_grant`, `confirmed_grant_session`, `draining` latch |

### Boot sequence (clerk side), `app/main.py`

1. `open_fleet_lane` (`fleet_boot.py:286-486`) runs expectation, then the volume gate,
   then `register` (`:407-426`).
   - `FleetLaneDraining` leads to `_learn_drain`, then a refusal (`:427-438`).
   - **Any `FleetPresenceError`** leads to the offline rule (`:439-485`). That rule
     refuses a foreign, absent or non-provisioned evidence file. Otherwise it boots
     with `session=None`.
2. The heartbeat and identity echo are installed **only if online** (`main.py:316-329`).
3. If online, `reserve_account` runs (`main.py:597-602`). If offline,
   `offline_boot_matches` (`fleet_boot.py:584-608`) runs against the current
   selection generation (`main.py:603-614`).
4. Custody opens (`select_active_clerk_runtime`, `main.py:618`), then the local
   durable acknowledgement (`acknowledge_runtime_binding`, `main.py:634-636`).
5. If online, `confirm_and_report` (`main.py:637-654`) calls `confirm_binding`. That
   reads `boot.session` once (`fleet_boot.py:526`), calls `presence.confirm`, and
   **then** writes the evidence (`:557-572`).
6. Boot recovery (`main.py:857`) and the reconciliation sweep (`main.py:919`) start
   **whatever the fleet status**. The sweep's reconcile runs the stuck-EXIT watchdog,
   which can submit reducing orders (`app/broker/alpaca/clerk/sqlite/reconcile.py:825`).

### Crash-point table

"Registry" and "Evidence" give each side's belief after the restart. "Restart" gives
what the restarted lane does, first with the coordinator reachable and then with it
unreachable.

| # | Process dies… | Registry | Evidence | Restart, coordinator reachable | Restart, coordinator unreachable | Converges? |
|---|---|---|---|---|---|---|
| K1 | after registry commits `register`, before the reply | session N+1 (an instance the process never adopted) | unchanged | registers N+2; N+1 archived (`service.py:1299-1300`) | offline rule | yes |
| K2 | after `reserve` commits, before the reply | RESERVED for this clerk | unchanged | same-owner re-reserve returns the row (`service.py:1399-1401` doc) | no reserve offline; FR-066 match on prior evidence only | yes |
| K3 | after custody or the local ack, before `confirm` | RESERVED, or EFFECTIVE from a prior gen, confirmed by a dead session | prior grant, or none | re-confirm under the new session: equal gen re-acks, higher gen advances (`service.py:1771-1803`) | the pinned generation (`main.py:608`) differs from prior evidence, so refuse. Same gen and tuple boots (correct: that grant *was* confirmed) | yes |
| K4 | **after the registry commits `confirm`, before the reply or the evidence write** (the audit's named crash) | EFFECTIVE, confirmed by instance N | prior grant or none | re-confirm the equal gen under N+1 (`:1771-1803`). Execution routing stays closed until then, because `confirmed_by_current_session` is false (`:2626-2642`) | evidence is older, so refuse (fails closed) | yes |
| K5 | inside `write_confirmation_evidence` | EFFECTIVE, gen G | old file, or new file (atomic rename). A stray `confirmation.json.tmp-<pid>` may remain (never cleaned; harmless) | as K4 | old file: refuse; new file: boot G | yes |
| K6 | after the evidence write, before `confirmed_grant` is set in memory | EFFECTIVE, gen G | gen G, provisioned | re-confirm, equal gen | FR-066 boots G (correct) | yes |
| K7 | a heartbeat re-registers mid-`confirm` (refused beat, `fleet_boot.py:1117-1125`) | the confirm names superseded session N, so it is refused `ClerkIdentityMismatch` (`service.py:1746-1760`) | unchanged | `confirm_and_report` raises inside the lifespan, startup aborts, the next boot converges | n/a | yes, via a restart (P3) |
| K8 | drain commits between `register` and `confirm` | draining | marked `draining` (`fleet_boot.py:543-556`) | refused, tombstone kept | tombstone refuses (`:465-478`) | yes |
| K9 | **drain commits after the registry commits `confirm` and before the reply lands, and a beat lands in that gap** | draining | the beat marks it `draining`, **then `confirm_binding` overwrites it with a `provisioned` file** (`:557-572`) | the register refusal re-marks (`:427-431`), so it recovers | **boots the drained binding offline** | only if a later online contact happens first. See **G-2** |
| K10 | the lane is down for the whole drain, then **force-retire** | retired; assignment released (`service.py:718-748`) | provisioned (never learned) | **404 → `FleetPresenceError` → offline boot** (D-1) | offline boot (ADR 0063's charted residual window) | **no** |
| K11 | the lane booted offline and the coordinator returns | anything | as booted | n/a: an offline lane never re-registers (D-2) | stays offline until a restart | **no** |
| K12 | a live lane is force-retired while it holds a position (its drain was learned) | retired; released | draining (correct) | the beat is refused 404, then re-registration is refused 404, logged every beat (`fleet_boot.py:796-800, 1132-1138`). **The process keeps custody and its existing bots** | same | **no.** See **G-3** |

## (b) Invariants each side assumes of the other

| # | Assumed by | Invariant | Guaranteed? |
|---|---|---|---|
| I1 | registry | Execution routes only to the session that confirmed | **Yes.** `_descriptor` (`service.py:2626-2642`) plus the stale projection (`:2992-2993`) |
| I2 | registry | A confirmation from a superseded session never lands | **Yes.** In-transaction fence (`service.py:1746-1760`) |
| I3 | registry | "Retired" is terminal and the lane never serves again (`service.py:2832-2838` message) | **No.** The lane treats the 404 as unavailability (D-1). A live lane is never stopped (G-3) |
| I4 | registry | Liveness never transfers ownership (ADR 0062, anti-patterns) | Registry side yes. But force-retire releases on the deadline alone (`service.py:693-748`), and nothing stops the old lane (D-1, D-2, G-3) |
| I5 | evidence | It is written only after the registry accepted (`fleet_boot.py:518-524`) | **Yes** (`:557` follows `:533`) |
| I6 | evidence | A learned drain is durable and not overwritten (`confirmation.py:78-83`) | **No.** An in-flight confirmation rewrites `provisioned` (G-2) |
| I7 | lane | Any `FleetPresenceError` means "coordinator absent", so FR-066 applies (`presence.py:53-62`) | **No.** Remote maps *every* non-draining 4xx into it (`presence.py:391-396`), including `clerk_not_found` for retired. The test `test_a_reachable_coordinator_that_refuses_expectation_boots_offline_too` (`tests/broker/fleet/test_a2_alpaca_lane.py:2376`) pins that reclassification on purpose. `LocalPresence` lets `ClerkNotFound` escape instead (`presence.py:224-238`), so **the two transports disagree** |
| I8 | lane | Offline is temporary, and "routing stays closed until the coordinator returns" (`fleet_boot.py:481-485`) | **No.** Nothing re-registers (D-2) |
| I9 | lane | `_learn_drain` failing leaves the lesson retryable (`fleet_boot.py:710-712`) | Only for the function. Its heartbeat callers let the raise kill the beat (D-3) |
| I10 | registry recovery | Lane evidence is the grant to restore (`app/broker/fleet/recovery.py:511-528`) | It ignores `lifecycle_state`, so a drained tombstone restores EFFECTIVE (G-6, adjacent to C6) |

## (c) Named suspected gaps

Severity uses the map's scale. "Paper" means the prototype needs the Alpaca paper
account.

- **G-1: the retired-lane zombie makes a second writer.** *Hypothesis:* a lane that was
  force-retired (K10), or that restarts after D-1, opens custody on an account the
  registry has released to a successor. Its reconcile sweep's stuck-EXIT watchdog
  (`reconcile.py:825` → `exit_watchdog.redrive_or_escalate_stale_exits`) then submits
  a reducing order into the successor's account.
  - **Severity:** P0.
  - **Prototype:** in-process. Seed the retired lane's SQLite with an aged
    EXIT_NOT_FLAT episode. Run D-1's restart against a fake trade port shared with a
    successor clerk, and count the submits that reach it.
  - **Paper:** no (a fake broker keeps the distinction). Confirming against Alpaca's
    real duplicate-order handling would need paper.
- **G-2: an in-flight confirmation erases the drain tombstone.** *Hypothesis:* a drain
  committing between the registry's `confirm` commit and the lane receiving its reply
  (remote transport only; `LocalPresence` has no await point) lets a beat mark the
  evidence `draining`. `confirm_binding` then writes `provisioned` over it
  (`fleet_boot.py:557-572`). The `draining` latch is already set, so `_learn_drain`
  never re-marks (`:708-709`). A later restart during a coordinator outage boots the
  drained binding (K9).
  - **Severity:** P1.
  - **Prototype:** a `RemotePresence` stub whose `confirm` awaits an event. Drain the
    registry and let one beat run, release the event, then assert the file's
    `lifecycle_state`.
  - **Paper:** no.
- **G-3: a live lane never learns its retirement.** *Hypothesis:* force-retiring a
  live lane that has not gone quiet leaves the process running. `observe` answers
  `404 clerk_not_found` (`service.py:1337-1342`), and the beat only logs and retries
  (`fleet_boot.py:796-800, 1132-1138`). Its already-running bots keep trading the
  released account, and the drained-lane gate blocks only *new* starts (`main.py:830`).
  - **Severity:** P0. The ceremony allows it: `retire_clerk` sends a non-quiet lane to
    force-retire (`service.py:579-583`), and force-retire checks only the deadline.
  - **Prototype:** in-process. Start a lane with a fake bot, drain it, advance past the
    deadline, force-retire, then assert whether the bot task and custody are still up
    after N beats.
  - **Paper:** no.
  - **Overlap:** touches C5 (charted-fixed). Worth folding into the C5 cross-seam
    prototype if that one does not already cover retirement.
- **G-4: the offline generation pin fails open.** *Hypothesis:* when the selection read
  raises, `_effective_binding_generation_now` returns `None` (`main.py:135-142`), so
  `evidence_vouches_for` skips the generation check (`confirmation.py:307`). A tuple
  that changed away and back then boots under old evidence.
  - **Severity:** P3. The binding install that precedes it almost certainly also fails
    on an unreadable selection.
  - **Prototype:** monkeypatch the selection read to raise and call the offline branch.
  - **Paper:** no.
- **G-5: a forward to an offline lane skips the identity fence.** *Hypothesis:* an
  offline lane never installs the served-identity provider, so a coordinator-token
  forward that pins the dead session's epoch executes without `_pin_mismatch`
  (`app/broker/fleet/agent_identity.py:333-344`). The coordinator can still route
  there for up to the 30 s staleness window after the old process died (K4 with
  `confirmed_by_current_session` still true for N). This is reachable when the lane
  reached the coordinator but was refused, as in D-1 or a rotated agent token, while
  the coordinator→agent token still works.
  - **Severity:** P2. Same binding, and the echo check rejects the response *after*
    execution, so the outcome is recorded as unknown.
  - **Prototype:** a real agent app booted offline plus a coordinator `LaneRouter` with
    a fresh session N, and one execution forward.
  - **Paper:** no.
- **G-6: registry recovery restores a drained lane as EFFECTIVE.** *Hypothesis:*
  `reconcile_restored_lane` (`recovery.py:511-528`) never reads `lifecycle_state`. A
  restore from a backup taken before the drain re-seats the assignment from a
  `draining` tombstone. The restored registry says provisioned, so the lane registers
  online and serves again, and the operator's drain is lost.
  - **Severity:** P2.
  - **Prototype:** in-process restore ceremony.
  - **Paper:** no.
  - **Overlap:** adjacent to C6 G5 (#2298). Hand it to that prototype rather than
    ticketing it separately.
- **G-7: a migration keeps stale evidence.** *Hypothesis:* the migration ceremony
  (`scripts/manage_broker_fleet.py:527-543`) skips the rewrite when evidence already
  carries the same generation. The file then keeps the old `registry_id` and
  `routing_epoch`, and `reconcile_restored_lane` later refuses it as "registry newer
  than evidence" (`recovery.py:580-591`).
  - **Severity:** P3. It fails closed.
  - **Prototype:** unit test.
  - **Paper:** no.

## (d) Defects proven by reading

- **D-1: a retired clerk boots offline against a reachable coordinator.** P1 (a gate
  fails open; a P0 escalation is suspected in G-1). **Filed #2320.**
  - Chain: `_refuse_registration_for_closed_lane` raises `ClerkNotFound` (404) for
    RETIRED (`service.py:2832-2838`). `_refuse` returns it flat (`app/routers/internal_fleet.py:245-251, 310`).
    `RemotePresence._post` maps every non-draining non-200 to `FleetPresenceError`
    (`presence.py:380-396`). `open_fleet_lane` catches it as unavailability
    (`fleet_boot.py:439`). Evidence the lane never marked (K10) passes `:445-478`, so
    it boots offline. `offline_boot_matches` is true, so custody opens (`main.py:603-618`).
  - Probe (throwaway, not committed; run on the host venv against temp SQLite): use
    the #2155 test helpers to provision, open, reserve and confirm. Close the lane,
    then `drain_clerk`, advance 30 days, and `force_retire_clerk`. Restart with
    `RemotePresence` routed over `httpx.ASGITransport` to the real internal router.
    Output: `REGISTRY clerk lifecycle: retired`; `VOLUME evidence lifecycle: provisioned`;
    `RESTART online: False | offline_reason: …Clerk … is retired; a retired lane never returns to service.`;
    `FR-066 offline_boot_matches: True`.
- **D-2: an offline-booted lane never rejoins the fleet.** P1 (the operator is shown
  `unreachable` for a lane that holds custody and runs the watchdog; the drain and
  retire lessons can never reach it). **Filed #2321.**
  - Evidence: presence (beat and echo) is installed only when `fleet_lane.online`
    (`main.py:316-329`). The beat skips a lane with no session (`fleet_boot.py:772-773`).
    The only other `register` call is the refused-beat repair (`:1118`), which a lane
    with no beat never reaches. The log line promises "until the coordinator returns"
    (`:481-485`). Custody, boot recovery and the sweep start regardless
    (`main.py:618, 857, 919`).
- **D-3: learning a drain can kill the heartbeat.** P2.
  - `_learn_drain` is called outside any inner `try` on the beat's success path
    (`fleet_boot.py:801-803`) and inside the `except FleetLaneDraining` handler
    (`:791-795`). A `ConfirmationEvidenceError`, or an `OSError` from the evidence
    rewrite, escapes to the outer `except Exception` and ends the beat (`:816-834`).
  - The latch stays false, so nothing retries. The lane never confirms lane quiet and
    exits only through force-retire with its evidence still `provisioned`. That feeds
    D-1 and G-1.
  - Listed only; not filed.

Proven P3: K7's startup abort on a mid-confirm re-registration, and the uncleaned
`confirmation.json.tmp-<pid>` (`confirmation.py:237`).

## Charted, not new

- The ADR 0063 residual window (lane down for the whole drain, then a restart during a
  coordinator outage) is documented in `docs/architecture/adrs/0063-draining-is-an-observed-lane-handover.md`
  §7.1, 2026-09-21 amendment. D-1 is *not* that window: there the coordinator is
  reachable. D-2 widens it.
