# Fleet lane visibility, account onboarding, and lane handover

Plan date: 2026-09-15. Owner decisions recorded in §1 are binding; this plan
is their argument, not a place to re-open them.

## 1. Decisions (owner, 2026-09-15)

- **D1 (#2110).** The shell renders **one mode badge per lane**, for N lanes,
  driven by `FleetDirectoryService.lanesOf('alpaca')`. Not two hardcoded.
- **D2 (#2110).** A lane whose mode cannot be determined renders as a **loud
  warning that says assume real money**. Grey "not configured" is banned — it
  reads as reassuring and is the current bug.
- **D3 (#2139).** Ship the missing **parent runbook**: one standardized
  "add an Alpaca account (paper or live)" procedure. No in-product activation
  button. The app surfaces the refusal; the operator runs the ceremony.
- **D4 (#2111).** Lane handover: the lane stops accepting new routed work,
  finishes any order already in flight, then hands over. **Open positions and
  running bots stay with the account**; the successor lane adopts them.
  Draining replaces today's typed proof token with an observed fact.
- **D5 (data).** Both clerk volumes and `PythonDataService/artifacts/` are
  disposable (~420 MB). `data-lake-volume/` and `learn-ai_pgdata` are NOT —
  the lake is files plus a Postgres catalog, and deleting the catalog orphans
  the files. The real-money account `318420190` has never traded (4 KB, no
  custody DB) and must stay brand new.
- **D6 (#2105).** Token rotation deferred by owner. Not in scope.

## 2. Corrected diagnosis for #2110

The tracker says the shell shows one lane's verdict as the installation's.
It does not. The coordinator answers `/api/brokers/alpaca/live-verdict`
itself (`app/routers/fleet_compatibility_reads.py:24` ->
`app/routers/brokers.py:726` -> `get_active_clerk_runtime()`), and the
coordinator process is not a clerk — `app/main.py:650` installs a runtime
only on a lane. Verified in the running app 2026-09-15: the read returns
**200 OK** on every 5 s poll with `configured_mode: "unconfigured"`, and the
header renders **"Mode unknown — Alpaca is not configured"** while
`alpaca-live-clerk` is up.

So the trust anchor is dead, not ambiguous, and "aggregate the lanes" was
never available — there is no lane verdict at the coordinator to aggregate.

## 3. Global constraints

- **Never edit app code in the main checkout.** `polygon-data-service`,
  `alpaca-live-clerk` and `alpaca-paper-clerk` bind-mount
  `/Users/inkant/learn-ai/PythonDataService/app` to `/app/app`. Every code
  task runs in a git worktree rooted under `/Users` (never `/tmp` — podman
  cannot mount it). Master is never checked out to a feature branch.
- Time is `int64 ms UTC` everywhere on the wire and at rest
  (`.claude/rules/temporal-rigor.md`).
- Frontend tests run on the HOST from the worktree with `node_modules`
  symlinked; `podman exec my-frontend ng test` grades the MAIN checkout and
  is a false green.
- Project-scope lint before every push: `ruff check PythonDataService/app/
  PythonDataService/tests/`, `npx eslint Frontend/src/ --max-warnings 0`.
- `thermo-nuclear-code-quality-review` before the FIRST push of each PR,
  dispatched to a fresh subagent (never self-review). Majors block; file
  size is never a finding.
- Any docs edit is followed by `pytest tests/contracts` (docs link contract).
- Contract regeneration serializes merges: a PR that regenerates the fleet
  operation catalog snapshot or the OpenAPI contract must merge before any
  other PR that would regenerate either.

## 4. Streams

### Stream A — per-lane mode badge (#2110). Blocks C and D.

- **A1 (backend).** Declare `live_verdict` as a lane-scoped operation in
  `app/broker/alpaca/clerk/fleet_adapter.py`, at
  `OperationReadiness.CONFIGURATION_ACCESS` so a lane that is up but refusing
  stays readable. Regenerate the catalog snapshot via
  `scripts/regenerate_fleet_operation_catalog_snapshot.py` and the OpenAPI
  contract. Tests pin the readiness class — an execution-readiness regression
  must redden.
- **A2 (backend).** The unscoped `/api/brokers/{broker}/live-verdict` stops
  answering from the coordinator's non-clerk runtime. Route it through the
  existing compatibility-retirement machinery rather than inventing a second
  refusal path.
- **A3 (frontend).** `AlpacaLiveVerdictService` becomes lane-keyed: a map of
  `clerk_id -> verdict`, sourced from `FleetDirectoryService`. One lane's
  failure never blanks another's badge (FR-093 already demands this of the
  directory; the verdict service must match).
- **A4 (frontend).** `alpaca-live-banner.component` renders one lane, taking
  the lane as an `input()`. The shell renders N. Implements D2: the
  cannot-determine state is a warning that says assume real money, not grey
  "not configured". Must pass AXE and WCAG AA — this is a header-level status
  region with colour-carried meaning, so the text must carry it too.

### Stream B — account onboarding runbook (#2139). Independent of A.

- **B1.** Write `docs/runbooks/add-an-alpaca-account.md`: one standardized
  procedure covering paper and live. Cross-links
  `alpaca-sqlite-clerk-recovery-and-cutover.md` (which calls itself a
  subprocedure of a parent that was never written) and
  `fleet-dev-two-lane-posture.md`. Does not duplicate their steps.
  Carries the two gotchas already proven on 2026-09-15: a WAL/SHM sidecar
  refuses planning and must be cleared by a real checkpoint, never a delete;
  and plan+apply must both run inside one broker-evidence capture window.

### Stream C — drain ceremony (#2111). Needs A1's readiness distinction.

- **C1 (design, Opus).** ADR for the lifecycle transition: what the registry
  must observe before `provisioned -> draining -> retired` is allowed, and
  what replaces `RELEASE_PROOF_TOKEN`. Today `release_assignment`
  (`service.py:1034`) gates on a typed string asserting the lane is offline
  and obligations clear — a check that cannot fire.
- **C2 (implementation).** Per D4. In-flight accounting already exists as
  `_inflight` in `app/broker/fleet/lane_runtime.py`.

### Stream D — wipe and re-activate. Owner-supervised, NOT a subagent task.

Runs AFTER A ships, so the badge proves itself: wiping the paper authority
returns that lane to `ACTIVATION_REQUIRED`, and the new per-lane badge should
display exactly that. Then the paper account is re-activated **by following
B1**, which is the only real test of the runbook.

Destructive and touching live infrastructure — executed with the owner in a
no-bot window, never dispatched.

### Stream E — follow-up batch (#2142, #2145, #2146, #2147, #2148).

Small same-shape independent fixes. ONE batched dispatch, reviewed as one
diff. #2141 (lifespan wiring test) is already `ready-for-agent` and rides
along. #2143 and #2144 are judgment calls — separate.

### Stream F — tracker hygiene (controller, not dispatched).

Correct #2110's description per §2. File the `GET /api/broker/health` 404
observed on every poll cycle in the running app.

## 5. Order

A -> (B in parallel) -> D. C and E run in parallel with A but merge after it,
because A1 regenerates both contracts and contract regen serializes merges.

## 6. Model assignment

Sonnet for A1-A4, B1, C2, E. Opus for C1 and for the final whole-branch
review. Thermo review: fresh subagent per PR, never the implementer.
