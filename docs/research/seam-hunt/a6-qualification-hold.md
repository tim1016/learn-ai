# A6 — Fleet qualification hold stream: does the operator see holds correctly?

Research ticket: tim1016/learn-ai#2294 (map #2276, seam A6). Static read at
`a14f1df1` (master, 2026-09-23). Every `file:line` below is at that SHA
(`PythonDataService/` is abbreviated `PDS/`, `Frontend/src/app/` is `FE/`).
Nothing was run against live services or containers.

## Answer

**The ticket's premise is wrong in two places. Once corrected, the answer is:
the go-live hold never fails open, but no operator surface shows it. The
surfaces that exist show a held lane as startable (P2, proven).**

1. **`hold_stream` is not a hold-state stream.** `PDS/app/routers/fleet_qualification.py:317-326`
   is a *capacity-contention probe* for the Compose qualification ceremony.
   It waits 1 s, emits one literal frame `data: qualification`, and ends. It
   is only mounted when role, random namespace, per-run secret and both
   fake-upstream URLs line up (`fleet_qualification.py:282-290`, `:109-123`).
   It is excluded from OpenAPI (`:291`), and every call needs the per-run
   secret, or it answers 404 (`:163-172`). Its only caller is the host
   harness (`PDS/scripts/run_broker_fleet_compose_qualification.py:424-452`),
   which reads the status code only. **It has no frontend consumer, by design.**
   That is not a gap. The "hold" in its name means "hold a connection open",
   not the go-live hold.
2. **The go-live hold (#2269/#2273) has no stream and no read surface at all.**
   It is a marker file at the clerk volume root
   (`PDS/app/services/go_live_hold.py:42-43`, `PDS/app/services/lane_go_live.py:94-128`).
   Only the lane start gate reads it, on each Start/Resume
   (`PDS/app/services/bot_runner.py:351-386`, wired at `PDS/app/main.py:826-837`).
   No endpoint, heartbeat, presence field, panel snapshot or SSE frame carries
   it. A repo-wide grep of `FE/` finds no hold reference outside the
   generated release/receipt types (`FE/api/broker.types.ts:3433`, `:3676`,
   `:14862`, `:16850`) and the operation catalog snapshot
   (`FE/fleet/fleet-operation-catalog.snapshot.json:330`). No Angular code
   calls those routes. Go-live is CLI-only (`PDS/app/installation_migration/golive.py:53-89`).

**The gate itself is sound and fails closed.** Every path either refuses the
start or leaves the marker in place: unreadable root, unreachable,
unreadable or unparseable marker, unresolvable clerk dir
(`go_live_hold.py:125-154`, `lane_go_live.py:117-128`). Release needs the
confirmation words (`PDS/app/schemas/lane_go_live.py:15`, `:42`) **and** a
passing bar check less than 15 min old in the same process
(`lane_go_live.py:226-242`). It writes the receipt before removing the marker
(`go_live_hold.py:225-246`). A real-money start cannot pass a held lane.

**What the operator sees is wrong in the safe direction.** Both *previews*
skip the lane start gates. Deploy and Resume probe them
(`bot_runner.py:594-595`, `:715-716`), but `preview_start_admission`
(`bot_runner.py:630-676`) and `preview_resume_admission`
(`bot_runner.py:688-707`) never do. The panel builds its Resume button and
health-card label from that preview. On a held lane, a stopped flat bot
therefore shows **"Flat Resume ready"** with Resume enabled. The deploy page
shows **"Start allowed"**. The click is then refused with a 409. The same is
true of a **drained** lane (#2155): the drain gate sits in the same tuple and
the previews skip it the same way. So this is a pre-existing class of defect
that #2269 inherited, not something #2269 introduced. Rated **P2**: the gate
holds, and the wrong display corrects itself on the first click. No P0/P1 is
proven, so no `bug` issue was filed.

## (a) Trace

### A. The probe the ticket named (`hold_stream`)

| Step | Where | What |
|---|---|---|
| Mount gate | `fleet_qualification.py:282-290`, `:109-123` | Router exists only when `role=="clerk_agent"`, namespace starts `compose:fleetqualification`, and secret + broker URL + market-data URL are non-empty. |
| Mounted | `PDS/app/main.py:1032` | `qualification_router_from_environment(...)`; `None` in production. |
| Auth | `fleet_qualification.py:163-172` | Constant-time secret compare on bytes; 404 otherwise. |
| Body | `fleet_qualification.py:320-326` | `StreamingResponse`, `text/event-stream`, sleeps 1 s, yields `b"data: qualification\n\n"`, ends. |
| Pool | `PDS/app/broker/fleet/lane_runtime.py:708-718` | The lane middleware takes a *stream* lease when the response start carries `text/event-stream`. That is exactly what the probe exists to contend. |
| Consumer | `run_broker_fleet_compose_qualification.py:427-452` | Holds one stream, expects a second one to get the typed 503 `fleet_lane_capacity_exhausted`. Reads status and body `reason` only. |

No Angular, relay or store sits on this path. The A-seam stream patterns
(end-frame freeze, epoch race, keepalive-masked stall, 1 MB relay cap) cannot
apply, because nothing renders this stream.

### B. The go-live hold, end to end

| Step | Where | What |
|---|---|---|
| Raise | `PDS/app/installation_migration/importer.py:240`, `:590-620` | `migrate-installation import` stages a `GoLiveHoldMarker` per restored clerk volume. |
| Marker shape | `go_live_hold.py:49-59` | `kind`, `schema_version=1`, `written_at_ms` (int64 ms, `≤ MAX_TIMESTAMP_MS`), `volume`, `source_commit`, `registry_id`. |
| Where the lane reads it | `lane_go_live.py:94-114` | `resolve_clerk_dir()`; a relative path raises → held (`:124-127`). |
| Read | `go_live_hold.py:125-154` | Absent → not held; anything else unclear → held, with a `problem_kind`. |
| Gate | `bot_runner.py:351-386` | Held → `RunAdmissionRefusedError` (409, `PDS/app/services/bot_runner_errors.py:73-74`) with `reason_code` `LANE_GO_LIVE_PENDING` / `LANE_GO_LIVE_HOLD_UNREADABLE` (`bot_runner_errors.py:78-80`). |
| Gate probed | `bot_runner.py:594-595` (deploy), `:715-716` (resume) | Before admission, per call. No cache, so a release lands on the next click (`bot_runner.py:535-539`). |
| **Gate not probed** | `bot_runner.py:630-676`, `:688-707` | Start preview and Resume preview. |
| Deploy error → wire | `PDS/app/services/broker_v2_panel/panel_deploy.py:210-219` → `PDS/app/routers/broker_v2_panel.py:119-154` | `outcome="conflict"` (409), `admission=None`, top-level `reason_code`, `next_action` = generic "Correct the deployment inputs or bot state…". |
| Resume error → wire | `PDS/app/services/broker_v2_panel/panel_data_source.py:625-634` | `ActionNotAvailableError("Resume is no longer available for this bot.")`, `why`=gate detail, `reason_code` from the gate. |
| Release | `PDS/app/routers/brokers.py:922-971` → `lane_go_live.py:213-249` → `go_live_hold.py:192-266` | Control-secret POST; 409 without a fresh bar check, 503 when unreadable or the release failed. |
| Bar check | `brokers.py:902-919` → `lane_go_live.py:144-204` | Historical SPY 1-min bars, `use_rth=True`, 5 D, 30 s bound. Pass stored in process global `_LAST_PASSING` (`:131`). |
| Routing | `PDS/app/broker/alpaca/clerk/fleet_adapter.py:188-203` | `lane_ibkr_bar_check` (GET, MARKET_STATUS_READ), `lane_go_live_release` (POST, BOT_ACTION, one-shot key); both reachable before binding confirmation. |
| Operator view | `golive.py:53-144` | CLI emits `lanes`, `bars-proven`, `old-machine-off-confirmed`, `lane-released` (with `was_held`), `complete`. |

### C. Where hold state *should* reach the UI, and what it shows

| Surface | Source of its "can start" | Shows on a held lane |
|---|---|---|
| Panel health card (`FE/components/broker/v2-panel/operator-lens/health-card.component.html:26`) | `panel_projection_service.py:163-182` ← `resume_admission` ← `preview_resume_admission` (`panel_data_source.py:357-365`) | "Flat Resume ready" / "Resume custody proof ready" |
| Panel Resume action | `PDS/app/broker/v2panel/action_policy.py:162-176` (`decision.allowed` → enabled, no blocker) | Resume **enabled** |
| Panel SSE (A2) | same projection, serialised into `BotPanelLiveSnapshot` | the same, streamed; the hold never enters the fingerprint, so raising or clearing it never publishes a frame |
| Deploy page admission column (`FE/components/broker/broker-deploy-page/deploy-start-admission.component.html:1-4`) | `previewStartAdmission` (`alpaca-deploy-workflow.component.ts:834-840`) ← `bot_runner.py:630-676` | "Latest backend Start check — Start allowed" |
| Deploy page after click | `alpaca-deploy-workflow.component.ts:843-846`, `:974-995`, `:1008-1010` | Error banner titled "State changed before launch" **beside** the same "Start allowed" card (see D-2) |
| Fleet gallery / directory / lane attention bell | no hold field (`grep go_live` over `PDS/app/broker/fleet/`: only a timeout comment at `internal_http.py:46`) | nothing |

## (b) Payload fidelity

The hold has no hand-mirrored type in `FE/api/broker-models.ts` (grep: zero
hits). All the typed shapes are **generated** (`broker.types.ts`), and they
match the Pydantic models field for field:

| Python field (model) | Python type | TS type (generated) | Match |
|---|---|---|---|
| `GoLiveHoldMarker.kind` | `Literal["learn-ai-go-live-hold"]` | `"learn-ai-go-live-hold"` | yes |
| `.schema_version` | `Literal[1]` | `1` | yes |
| `.written_at_ms` | `int` (`0..MAX_TIMESTAMP_MS`) | `number` | yes (int64 ms, not string) |
| `.volume` / `.source_commit` / `.registry_id` | `str` | `string` | yes |
| `LaneGoLiveReleaseReceipt.released_at_ms` | `int` | `number` | yes |
| `.marker` | `GoLiveHoldMarker \| None` (always present) | `… \| null` (required) | yes |
| `.marker_problem` | `str \| None` | `string \| null` | yes |
| `.bar_check` | `LaneIbkrBarCheckRead` | same | yes |
| `LaneIbkrBarCheckRead.*_ms` | `int` | `number` | yes |
| `LaneGoLiveReleaseRequest.old_machine_off_confirmation` | `Literal["the old machine is off"]` | same constant | yes |
| Deploy 409 `detail.reason_code` (`broker_v2_panel.py:150-152`) | `str \| None` | **absent** from the hand-narrowed cast at `alpaca-deploy-workflow.component.ts:976-983` | dropped (P3, G5) |
| Deploy 409 `detail.admission` | `None` for a lane gate | cast `as RunAdmissionDecision \| undefined` (`:964`) | `null` handled by `??` |
| `PanelActionErrorResponse.reason_code` | `str \| None` | generated | yes |
| `GoLiveHoldState.problem_kind` | 4-member Literal | — | never serialised; no wire |
| `hold_stream` frame | the bytes `data: qualification` | — | no TS consumer, by design |

No int-vs-float, null-vs-absent or enum drift was found. The one dropped field
is the deploy error's top-level `reason_code`, which lives outside any model.

## (c) Stream behaviour

- **Reconnect / replay / `Last-Event-ID`:** not applicable. The probe stream
  is single-shot with no ids. The hold has no stream.
- **Ordering against a concurrent REST read:** the only REST reads that carry
  hold-derived state are the two previews, and they do not read the hold. So
  there is no ordering to get wrong. The display is wrong regardless of order.
- **Hold raised mid-stream:** import runs on a host whose stack is being
  restored. A marker appearing under a running lane changes nothing on screen.
  The next Start/Resume click is refused (`bot_runner.py:356-358` re-reads per
  call). Running bots are deliberately untouched (`go_live_hold.py:7-10`).
- **Hold cleared mid-stream:** likewise invisible. The next click succeeds
  without a restart (`test_go_live_hold_start_gate.py:77`).
- **Error frames:** none; refusals are HTTP 409/503 bodies on the command.
- **Auth expiry:** the probe carries a per-run secret on each request. Release
  needs the data-plane control secret (`brokers.py:925`). The bar-check GET
  has no route-level secret dependency (`brokers.py:902`); it is read-only and
  reached through coordinator routing.
- **A-seam patterns:** none reproduce here, because nothing streams the hold.
  A2's keepalive-masked stall (#2326) would *also* hide a hold change if the
  hold were ever put into the panel fingerprint. A fix for G1 below must add it
  to the fingerprint, or it will inherit A2's "emits only on change" behaviour.

## (d) Named suspected gaps

**G1 — "Held lane shows Resume ready" (P2, PROVEN — see D-1).**
Hypothesis: on a lane with `go-live-pending.json` (or a drained lane), a
stopped flat bot's panel shows "Flat Resume ready" with Resume enabled, and
the deploy page shows "Start allowed". The click answers 409.
Prototype sketch: host venv, `BotTaskRegistry(lane_start_gates=(go_live_start_gate(lambda: GoLiveHoldState(held=True, marker=...)),))`
with a fake start/resume admission that allows. Assert that
`await preview_resume_admission(...)` returns `allowed=True` while
`resume_existing_with_admission(...)` raises `RunAdmissionRefusedError(reason_code="LANE_GO_LIVE_PENDING")`.
Repeat with the drained gate. Then feed the preview into
`panel_projection_service._health_card` and `action_policy._guard_resume` and
assert the label and the enabled flag.

**G2 — "No read surface for go-live state" (P2, suspected / design gap).**
Hypothesis: after import, an operator in the UI cannot learn that a lane
awaits go-live, or that it cannot read its hold (`LANE_GO_LIVE_HOLD_UNREADABLE`),
except by clicking Start and being refused. After go-live, they likewise
cannot confirm a lane was released (`was_held`) outside the CLI transcript.
The fleet directory, presence and attention bell carry no hold field.
Prototype sketch: enumerate every `BotPanelLiveSnapshot`,
`LaneAttentionItem` and fleet directory schema field; assert none varies with
the marker (write, then remove it under a tmp clerk dir and diff the
projections). A fix would add a lane-level fact (a `lane_start_gate` blocker)
to the panel and to lane attention. Note the A2 fingerprint caveat above.

**G3 — "Bar check proves the historical farm, not the live feed" (P2, suspected).**
Hypothesis: `check_ibkr_historical_bars` passes when HMDS answers 5 days of
RTH SPY history (`lane_go_live.py:164-173`). IBKR serves historical and live
data from separate farms, so a lane whose live subscription gets no bars
(market-data farm down, missing entitlement, the `use_rth=False` live path
that bots use) still passes. Go-live then releases a lane the docstring says
was proven to "deliver bars" (`lane_go_live.py:7-13`). Worst case: bots start
and FEED_DEATH or stall. It self-corrects through feed health, but inherits
the extended-hours stall blindness of #2299/#2313.
Prototype sketch: fake `get_client()` with `is_connected()=True`, and
`fetch_historical_minute_bars` returning one bar while the realtime-bars
subscription yields nothing. Assert the check passes and release succeeds.

**G4 — "Confirmation delay voids the bar checks" (P3, suspected).**
Hypothesis: the CLI proves bars on every lane (`golive.py:66`), then waits on
a human prompt (`:68`). If the operator takes more than 15 min
(`BAR_CHECK_FRESHNESS_MS`, `lane_go_live.py:53`), the first release answers
409 `lane_go_live_bar_check_required`, and go-live refuses with nothing
released. It fails closed. But the operator just gets the lane's 409 text
("has not passed an IB Gateway bar check… or restarted since"), a few seconds
after the CLI printed `bars-proven` for that very lane. Nothing tells them the
delay caused it.
Prototype sketch: `run_go_live` with a fake `GoLiveLanes` whose release
raises the 409 refusal after an injected clock advance of 16 min inside
`confirm`. Assert the message names the delay.

**G5 — "Deploy refusal drops `reason_code` and mislabels the cause" (P3, PROVEN — see D-3).**

**G6 — "Restored volume the coordinator does not list stays held silently" (P3, suspected).**
Hypothesis: `run_go_live` releases only lanes in the coordinator directory
that are not retired (`golive.py:57`). A restored volume whose lane is absent
stays held. The `complete` step says so in general words (`:83-87`) but never
names which volumes import staged (`importer.py:273` `go_live_holds`) and
which remain. The operator is not shown the specific held lanes.
Prototype sketch: import manifest with 3 volumes, directory listing 2. Assert
`complete` names the third volume.

## (e) Defects proven by reading

**D-1 (P2) — Start and Resume previews skip the lane start gates, so a held or
drained lane is displayed as startable.**
`bot_runner.py:594-595` and `:715-716` probe `self._lane_start_gates`.
`preview_start_admission` (`:630-676`, docstring "Project the same Start
decision used immediately before mutation") and `preview_resume_admission`
(`:688-707`, "Project the exact new-run Resume decision") do not. Consumers:
`panel_data_source.py:363` → `panel_projection_service.py:163-173` ("Flat
Resume ready") and `action_policy.py:175-176` (Resume enabled). Also
`panel_deploy.py:245` → `alpaca-deploy-workflow.component.ts:834-840`
("Start allowed", then `deployBot` is called and refused). The tests pin only
deploy/resume refusal (`PDS/tests/services/bot_runner/test_go_live_hold_start_gate.py:61`,
`test_drained_lane_start_gate.py:34`); neither covers the preview. Severity
P2, not P1: the gate never fails open, and the false "ready" corrects itself
at the first click. **Owner to confirm.** If the owner treats "Resume ready on
a lane that awaits go-live" as false live state an operator acts on (P1 by the
map's wording), this becomes a `bug`.

**D-2 (P2) — After a lane-gate refusal the deploy page shows "Start allowed"
next to the error.**
`submit()` sets the preview decision (`alpaca-deploy-workflow.component.ts:839`).
The deploy is then refused with `admission: null` (`panel_deploy.py:216-217`),
so `admissionFromError` returns `null` (`:962-966`) and the catch keeps the
stale "allowed" decision (`:844-845`). The screen then shows the
`deploy-start-admission` card "Start allowed" (`deploy-start-admission.component.html:4`)
beside the error banner. The banner's title is "State changed before launch"
(`:985`, `:1009`), which is also wrong: the hold existed before the preview.
It is a consequence of D-1, but a separate fix: clear the decision when the
refusal carries none.

**D-3 (P3) — The deploy refusal's machine code and next step are lost.**
`broker_v2_panel.py:150-152` sends a top-level `reason_code`
(`LANE_GO_LIVE_PENDING`). The hand-narrowed cast at
`alpaca-deploy-workflow.component.ts:976-983` omits it, so it is never shown
through `receiptLabel`. `next_action` is the generic "Correct the deployment
inputs or bot state, then submit a new command." (`panel_deploy.py:214`),
which contradicts the gate's own `detail` ("Run `python -m
scripts.migrate_installation go-live`…", `bot_runner.py:379-382`). The Resume
path has the same generic headline, "Resume is no longer available for this
bot." (`panel_data_source.py:627`). It implies a state change, though the hold
predates the panel render.

## Invariants each side assumes

| Assumer | Assumes | Guaranteed? |
|---|---|---|
| Panel / deploy page | the preview decision equals what Start/Resume will decide ("the same decision", `bot_runner.py:647`, `:693`) | **No**: the lane gates run only on mutation (D-1). |
| Operator (CLI) | a released lane was held and is now free | Yes, per lane, via `was_held` in each receipt (`golive.py:136-142`). Not for lanes left unlisted (G6). |
| Gate | the marker lives at `resolve_clerk_dir()` | Pinned by `tests/installation_migration/test_go_live_hold_root.py`. |
| Release | a passing bar check means the lane receives live bars | **Partly**: historical farm only (G3). |
| Qualification harness | `/hold/stream` takes a stream-pool lease | Yes: headers go out before the 1 s sleep, and the middleware keys on `text/event-stream` (`lane_runtime.py:708`). |

## Bugs filed

None. No P0/P1 is proven. D-1 is the owner-decision candidate for P1.
