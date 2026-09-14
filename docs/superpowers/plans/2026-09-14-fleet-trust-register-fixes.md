# Fleet Trust Register Fixes — Implementation Plan (master)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This master document sequences seven lane plans; each lane file carries its own tasks, failing tests, implementations and commits.

**Goal:** Close the 18 issues filed from the fleet trust audit (#2063–#2077, #2079, #2080, #2082) in an order that never drops a live clerk mid-session, never severs the market-data source, and lands the highest-value fixes first.

**Architecture:** Seven lanes with disjoint files run in parallel; within a lane, tasks are sequential. Lane 0 lands the two open PRs. Every code lane produces small PRs with a regression test proven failing-before/passing-after. Host operations (restart, topology migration, qualification run) are scheduled into no-bot windows.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest (Frontend), Podman Compose on macOS (applehv), GitHub Actions.

**Spec:** GitHub issues #2063–#2082 (full text captured 2026-09-14 07:27 CDT), `docs/broker-clerk-fleet-authority.md` §6 trust register (on the #2078 branch), and the seven lane plans in this directory:

| Lane | File | Issues |
|---|---|---|
| A | `2026-09-14-fleet-lane-a-stream-resilience.md` | #2079, #2082 |
| B | `2026-09-14-fleet-lane-b-ibkr-market-data.md` | #2080, #2077 |
| C | `2026-09-14-fleet-lane-c-boot-and-gates.md` | #2064, #2063, #2073, #2071 |
| D | `2026-09-14-fleet-lane-d-routes-and-posture.md` | #2069, #2075, #2076 (backend) |
| E | `2026-09-14-fleet-lane-e-frontend-fence-and-refusals.md` | #2068, #2067, #2076 (frontend) |
| F | `2026-09-14-fleet-lane-f-topology-and-qualification.md` | #2066, #2070, #2072 (framing) |
| G | `2026-09-14-fleet-lane-g-test-integrity.md` | #2074 |

## Global Constraints

Copied from CLAUDE.md, `.claude/rules/*.md`, and the memory notes that bind this work. Every lane task inherits these.

- **Never disable `IBKR_BROKER_ENABLED`.** It installs the shared `MarketDataFeed` that Alpaca bots stream decision bars through (`app/main.py:735`, `bot_runtime.py:156`). The IB Gateway is live market-data machinery; only IBKR order actuation was retired.
- **Never `git checkout` / `switch` / `stash` in `/Users/inkant/learn-ai`.** Both clerks bind-mount `./PythonDataService/app` from this checkout. Do feature work in worktrees under `/Users/inkant/learn-ai-worktrees/` (Podman cannot see `/tmp`). Leave the main checkout on master.
- **Every `./restart.sh` runs `podman compose down`** and drops both clerks and any running bot. Deploy only in a no-bot window: after 15:00 CT or before the first launch on a trading day, or on a weekend. Import-time changes (`app/main.py`, `fleet_boot.py`, settings, compose env) take effect only after a restart.
- **Delivery protocol per PR** (the AFK protocol from #1642): branch → regression test verified failing on master → project-scope lint → targeted tests plus every suite that consumes a shared helper you edited → thermo-nuclear review **once**, run by an independent subagent, every major finding fixed → push → wait 30 minutes for CI and CodeRabbit → fix what arrived → merge if green. Re-pushes do not re-trigger thermo. One PR body per PR, ending with the Claude Code attribution line.
- **Commands.** Python: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/<targets> -q`. `tests/broker/fleet` binds sockets and FAILS under the Bash sandbox; run it with `dangerouslyDisableSandbox: true`. Lint: `ruff check PythonDataService/app/ PythonDataService/tests/`. Frontend: scoped runs via `npx ng test --watch=false --include='src/app/path/exact.spec.ts'` (never a directory glob), and a **full** `npx ng test --watch=false` before every frontend push because parent specs pin child copy. C#: not touched by this plan.
- **Generated artifacts.** Any Pydantic schema or route change regenerates `contracts/openapi/python-data-service.openapi.json` (`python scripts/export_openapi_contract.py`) and `Frontend/src/app/api/broker.types.ts` (`npm run codegen:openapi` in `Frontend/`) in the same commit, or the contract CI jobs fail. Two PRs that both regenerate must be sequential.
- **Docs edits** (anything under `docs/`, `CONTEXT.md`, runbooks) must pass `pytest tests/contracts` (link contract) before push.
- **Time is `int64 ms UTC`** on every wire and in every stored field. No ISO strings, no `datetime` as a wire type. New timestamp fields are named `*_ms`.
- **No silent exception handlers, no `print()`, structured logging with `extra={"action": ...}`.**
- **Secrets.** `compose.override.yaml` and `deploy/fleet/env/*.env` hold live credentials. Never cat them, never paste values into a PR, an issue, a test, or a chat message. Report key names only.

---

## What changed since the issues were filed (read before executing any lane)

The seven investigations verified every `file:line` claim. These corrections change what gets built:

1. **#2080's premise is wrong.** The IB Gateway is not decommissioned. Both clerks connected to it at 12:34 UTC today and installed it as the shared feed. The overnight churn was the gateway being logged out overnight. The fix is a log budget plus an outage anchor, not a client kill switch. **Owner action:** enable IB Gateway auto-restart. A gateway still logged out at the first RTH decision trigger finalises a running bot as `FEED_DEATH` (`ibkr_continuity.py:346-365`).
2. **#2077 misnames two of its three "misnamed" variables.** `IBKR_READONLY` is a real `ib_async` connect parameter. `IBKR_LIVE_BARS_ROOT` holds genuine IBKR bars in a lane-local directory (misplaced, not misnamed). Only `IBKR_LIVE_RUNS_ROOT` carries Alpaca data under an IBKR name. Default: document, do not rename now.
3. **#2069's "delete the unscoped mutations" would sever fleet routing.** 22 of the 27 unscoped mutation routes are the agent transport a catalog `agent_path_template` forwards to. Only three are orphans with zero callers. Worse and unfiled: the compatibility-retirement 410 already matches 15 live catalog reads, so the first Delivery-E ceremony on a clerk would 410 the coordinator's own forwarded configuration and status reads.
4. **#2068's freeze-at-open is a correctness requirement, not UX.** The backend fence can only refuse a stale generation the client sends; a click-time read always sends the current one. The bug is dormant only because `refresh()` has no caller. Adding a caller before the freeze would activate it.
5. **#2067 undercounts.** The closed refusal set is 29 codes (22 declared, 3 minted inline, 4 more subclasses outside `errors.py`), and the `next_step` gap is 95 of 184 sites across five zero-coverage families.
6. **#2073(c) is half wrong.** `RemotePresence` passing no `volume_root` is by design (ADR 0062 addendum 6: the coordinator never inspects an agent-local path). The real gap is `LocalPresence`, which shares the filesystem and could always re-prove the root.
7. **#2064's third writer is on the wrong volume.** `fleet_boot.py:159-162` opens the coordinator's control volume, not the clerk volume, and it is structurally unavoidable (the gate's expectation comes from that registry). The profiles DB and the installation lock file are the two real violations.
8. **#2066's env files already exist and are already referenced.** The override's inline `environment:` literals outrank `env_file`, so the fix is deleting four literals, not adding files. The reviewed `compose.fleet.yaml` renames the project and every custody volume; deploying it would orphan the Live custody store.
9. **#2075's cited path does not exist.** The guard is `app/security/data_plane_control.py`; the test asserting 200 is `tests/broker/fleet/test_b_scoped_contracts.py:644`.
10. **#2074 estimated ~37 divergences; 33 were found with line numbers**, plus two structurally unfirable canaries in the custody sweep and a third in the retirement test.
11. **Lane A's counter trap is worse than the issue says.** Resetting the counter that also feeds the reconnect budget turns six existing tests into infinite loops, not failures. The two-counter split is what avoids it.

---

## Lane 0 — land today (2026-09-14)

State at 07:27 CDT: PR #2078 and PR #2081 both `MERGEABLE`, merge state `CLEAN`, every check green, no reviews pending. Containers restarted by the owner at 07:34 CDT on master `e2fdb23f`; all seven services healthy; both clerks connected to the IB Gateway. The running code does **not** include #2081's fixes.

- [ ] **Step 1: Merge #2078 (docs authority), then #2081 (production safety).** Both are independent; #2078 first so later docs branches start from it.

```bash
gh pr merge 2078 --squash --delete-branch && gh pr merge 2081 --squash --delete-branch
```

- [ ] **Step 2: Close #2065 by hand** (the #2081 body carries no `Closes` line) with a comment pointing at the merged PR.

- [ ] **Step 3: Comment on the four partially fixed issues** so the remaining halves are unambiguous: #2067 (parser half fixed; vocabulary, `next_step` backfill and bare-detail wrapping remain → Lane E/D), #2068 (cold-load retry fixed; freeze-at-open and conflict remain → Lane E), #2076 (coordinator heartbeat warning fixed; the rest → Lanes D and E), #2066 (lost-config warning fixed; topology remains → Lane F).

- [ ] **Step 4: Pull master on the main checkout** (owner, on the host) and schedule the first restart window. The restart loads #2081's restart.sh triage, the refusal parser, the market-status backoff reset and the middleware scoping. **Window:** after 15:00 CT today with no bot running, or before the first launch tomorrow.

- [ ] **Step 5: Owner enables IB Gateway auto-restart** (Configure → Settings → Lock and Exit → Auto restart). This is the only fix for the overnight blackout killing a bot; nothing in code can do it.

---

## The 18 issues — classification

| Issue | After #2081 | Kind | Lane / PR | Default decision |
|---|---|---|---|---|
| #2063 validate_served_context | open | decision + fix | C, one PR | wire it |
| #2064 profiles DB before gate | open | boot-order fix | C | reorder; subprocess boot test |
| #2065 restart.sh | **fixed** | close | 0 | — |
| #2066 topology gitignored | safe half fixed | commit + migrate | F | transcribe running override; creds env_file-only |
| #2067 refusal rendering | parser half fixed | vocabulary | E (PR B/C) + D | snapshot pattern, not OpenAPI |
| #2068 fence re-read | retry fixed | freeze-at-open | E (PR A) | freeze everywhere; conflict on drift |
| #2069 unscoped mutations | open | targeted delete | D (PR A/B) | delete 3 orphans; exempt coordinator forwards |
| #2070 fault matrix unrun | open | run-once + CI | F | accepted residual; precondition for Paper |
| #2071 fence copy | open | rename | C | one hold, `mutations_closed` |
| #2072 audit read surface | open | decision | F §6 | one coordinator GET, later slice |
| #2073 fences untested | open | tests + DDL | C | negative tests; UNIQUE + trigger + txn |
| #2074 test integrity | open | tests-only | G, three PRs | fakes are evidence |
| #2075 CONTEXT.md false | open | docs + posture | D (PR C) | refuse unpinned on clerk_agent |
| #2076 minor cleanups | heartbeat fixed | cleanups | D (PR D), E (PR C/D) | fix 7 backend, defer 2 |
| #2077 IBKR residue | open | docs; rename deferred | B (PR B) | keep authority canonical, amend |
| #2079 trade_updates backoff | open | fix | A (PR 1) | two counters; reset on connected |
| #2080 IBKR reconnect churn | open | fix | B (PR A) | log budget + outage anchor |
| #2082 snapshot RemoteDisconnected | open | fix | A (PR 2) | one GET/HEAD retry, logged |

---

## Owner decisions — defaults apply if unanswered

Each line is one plain question with the default the lanes plan against. Answer only the ones you want to flip.

1. **#2063 — keep the provider gate or delete it?** Default: keep it and wire it in (12 lines, negative test). Flip: delete + amend ADR 0062 Decision 5.
2. **#2069 — delete the three dead unscoped bot routes?** Default: yes (`POST .../bots`, `.../bots/{sid}/stop`, `.../bots/{sid}/actions`). Two stranded operator routes (replay-receipt, loss-hold clear) stay and are documented.
3. **#2071 — one recovery fence or two?** Default: one; rename the CLI key to `mutations_closed` and say so in the ADR.
4. **#2075 — should a clerk agent refuse unpinned mutations in code?** Default: yes, on `clerk_agent` only; `combined` unchanged.
5. **#2077 — rename `IBKR_LIVE_RUNS_ROOT` now?** Default: no; document it as a deliberate legacy name. The IBKR authority doc stays canonical and gains a "market data is load-bearing for Alpaca" section.
6. **#2070 — run the fault matrix once on the host?** Default: yes, outside market hours, and add a nightly CI run. It is a precondition for Paper activation, not for the next Live launch.
7. **#2066 — commit the running topology or deploy the reviewed one?** Default: commit a transcription (`compose.fleet.dev.yaml`), same project name, same volumes. Containment hardening (read-only rootfs, pids limit, tmpfs, drop the bind mount) in a second window a trading day later. Private network move last.
8. **#2072 — give the audit trail a read surface?** Default: yes, one coordinator-only `GET /api/broker-clerks/audit/routing-receipts?since_ms=` behind the existing control secret. Not scheduled in this plan; file it.
9. **#2067 — export refusal reasons through OpenAPI?** Default: no; committed-snapshot parity (the `test_vocabulary_snapshot.py` pattern). The backend wraps its own bare-detail 403/503 into the flat refusal body.
10. **#2068 — freeze the binding generation at open everywhere and surface drift as a conflict?** Default: yes, one shared helper, three surfaces.
11. **#2080 — change the reconnect cadence?** Default: no. Only the logging and the health field change.
12. **#2082 — retry budget?** Default: one retry, GET/HEAD only, explicit wrapper (not urllib3 `Retry`), logged.
13. **#2076(d) — bridge `order-groups` or delete the dead frontend method?** Default: delete `listOrderGroups` (no production caller). Flip: Lane E Task 11 bridges it.
14. **#2074 — are the fake providers evidence or smoke?** Default: evidence; the fakes diverge and `fake_beta` gains a `CONFIGURATION_ACCESS` operation.

## Owner-only actions (not code)

- Enable IB Gateway auto-restart (above). Log into the gateway before each session until then.
- Copy `compose.override.yaml` to a 0600 file outside the repo before the Lane F migration. It is currently the only durable copy of five secrets.
- Answer decisions 1–14 above, or let the defaults stand.

---

## Waves — what runs when

Files are disjoint across lanes except where noted under "Ordering rules". Within a wave, PRs are parallel.

### Wave 1 — start now, no boot changes (parallel)

| PR | Lane | Content | Deploy needs |
|---|---|---|---|
| A-1 | A | #2079 two-counter reconnect | restart |
| A-2 | A | #2082 GET retry on dead pooled connection | restart |
| B-A | B | #2080 outage anchor + log budget (Tasks 1–2; regenerates OpenAPI) | restart |
| B-B | B | #2077 docs: IBKR authority amended, setup guide, config comments, served-doc fix (Task 3; branch after #2078 merges) | none |
| D-A | D | exempt proven coordinator forwards from retirement 410 (Task 1) | restart |
| E-A | E | freeze-at-open `laneFence`, roster + panel + drawer conflict, `refresh()` guard last (Tasks 1–6; depends on #2081 merged) | frontend rebuild |
| F-1 | F | `.gitignore` widen + secret-free compose contract, `compose.fleet.dev.yaml`, render snapshot + CI gate, `restart.sh -f` (Tasks A1–A4; A3 edits `ci.yml`, rebase on #2078) | none until migration |
| F-2 | F | qualification test fix + nightly CI job (Tasks B3–B4) | none |
| G-1 | G | fakes diverge + blast radius + `CONFIGURATION_ACCESS` (T1–T3b) | none |
| G-2 | G | three flake fixes (T4–T6) | none |

### Wave 2 — boot and gates (one sequential PR, deploy with a registry backup)

| PR | Lane | Content | Deploy needs |
|---|---|---|---|
| C-1 | C | Tasks 1–7 in order: gate before writers; `_fence_writable_roots` negative test; wire `validate_served_context`; `LocalPresence` structural gate; schema v3 nested-root DDL + transaction; one recovery hold; lint + thermo | **restart in a no-bot window, after `manage_broker_fleet backup-registry`, after the duplicate-root precheck against a copy of the registry** |

### Wave 3 — routes, posture, cleanups (after C-1; sequential because of generated contracts)

| PR | Lane | Content | Deploy needs |
|---|---|---|---|
| D-B | D | delete three dead unscoped bot mutations + absence contract + route-inventory doc (Task 2; regenerates OpenAPI + `broker.types.ts`) | restart |
| D-D | D | seven backend cleanups incl. the agent-path-resolves contract test and `bots_deploy_apply` removal (Task 5; regenerates contracts) | restart |
| D-C | D | refuse unpinned mutations on `clerk_agent` (Task 4) then CONTEXT.md correction (Task 3) | **restart; verify each lane's coordinator token is provisioned first** |
| G-3 | G | B-scoped fixtures made falsifiable + 33 renames/strengthenings (T7–T11; rebase on D-C, both edit `test_b_scoped_contracts.py`) | none |

### Wave 4 — refusal vocabulary and catalog paths (after D-D)

| PR | Lane | Content | Deploy needs |
|---|---|---|---|
| E-B | E | `refusal_vocabulary.py` + snapshot + generator + CI step + `next_step` backfill for five families + router wraps bare-detail 403/503 (Task 7) | restart |
| E-C | E | frontend refusal copy map + contract test; render `authority_state` (Tasks 8–9; imports E-B's snapshot) | frontend rebuild |
| E-D | E | catalog snapshot + `operationUrl` builder + 44 call-site migration; delete dead `listOrderGroups` (Task 10, Task 11 per decision 13; generate the snapshot after D-D) | frontend rebuild |

### Wave 5 — host operations (owner at the keyboard, no bot running)

| Step | Lane | Window |
|---|---|---|
| Restart to load Waves 1–4 (can be several windows) | — | after 15:00 CT or before first launch; weekend preferred for C-1 |
| Reclaim the three `fleetqualification*` volumes (B1) — never `volume prune` | F | any time, unhurried |
| Topology migration M1–M9 (A5): backup override, move secrets to env files, retire fleet content from the override, render + `--check`, restart, verify SAME volume names and SAME markers | F | ~45 min, no bot, after F-1 and #2081 merged |
| One-time qualification run (B2) with the three `FLEET_*_ENV_FILE` exports pointing at an empty file | F | ~30 min, outside market hours; record evidence in the authority doc §4/§6 |
| Containment hardening PR (read-only rootfs, tmpfs, pids limit, drop bind mount) | F (unplanned here) | a separate window ≥1 trading day after the migration |

### Later, not scheduled

- #2072 first slice (decision 8) — one route, one store parameter.
- #2077 rename slice (Lane B Task 4) only on an explicit owner override.
- `draining` lifecycle ceremony and `_PRIVATE_HOST_CACHE` TTL (Lane D deferrals).
- Per-lane `live-verdict` (Lane E Task 12 deferral; backend redesign).
- Re-read the trust register the day the Paper lane activates.

---

## Ordering rules (where lanes touch the same file)

- `app/main.py`: C-1 rewrites `lifespan`/`_service_lifespan` (lines ~206–393, 849); D-C edits the middleware install (~977, on top of #2081); B touches nothing in `main.py`. Land C-1 before D-C.
- `app/broker/fleet/routing.py` and `errors.py`: C-1 Task 3 (`_resolve`, docstring) before E-B (`next_step` backfill at the raise sites).
- `app/broker/alpaca/clerk/fleet_adapter.py`: C-1 Task 3 (docstring) → D-D Task 5 (`bots_deploy_apply` delete, `provider_summary` fields, adapter version bump) → E-D Task 10 (snapshot generated last).
- `app/broker/fleet/lane_runtime.py` / `test_lane_runtime.py`: D-A adds a test; G-2 T5/T6 edit the same test file. Either order, rebase.
- `tests/broker/fleet/test_b_scoped_contracts.py`: D-C Task 4 (harness parameter) and G-3 T7–T9. Land D-C first.
- `tests/broker/fleet/test_import_isolation.py`: D-D Task 5(c) renames the phase-2 test and deletes `production_adapter()`; G item 19 asserts absence. D-D wins.
- Generated contracts (`python-data-service.openapi.json`, `broker.types.ts`): B-A, D-B, D-D, E-B, E-D each regenerate. Never two in flight unrebased.
- `.github/workflows/ci.yml`: #2078 (−24 lines), F-1 A3 (new job), E-B (extends the vocabulary job). Rebase in that order.
- `docs/doc-authority.md`, `docs/runbooks/ibkr-setup-guide.md`: #2078 edits both; B-B branches after #2078 merges.
- Lane B's `models.py` change alters `/api/broker/health`'s wire shape; deploy Backend/Frontend consumers in the same window.

---

## Verification (per PR, before push)

```bash
ruff check PythonDataService/app/ PythonDataService/tests/
```

```bash
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet tests/contracts -q
```

Run the line above unsandboxed. Baseline on master today: 315 passed, 0 failed (325 with #2081). Add the lane's own targets from its plan file, then the bounded gate:

```bash
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.run_fast_tests
```

Frontend PRs additionally run the full suite:

```bash
cd Frontend && npx ng test --watch=false
```

Any PR touching `docs/`, `CONTEXT.md` or a runbook:

```bash
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/contracts -q
```

---

## Risks carried forward (the ones that change what you do)

1. **C-1 Task 5's UNIQUE index can refuse to install on the live registry** if two non-retired clerks share a `volume_root` in one namespace; the registry then does not open and every lane loses routing. Run the `GROUP BY volume_root HAVING count(*) > 1` query against a copy first (Lane C Risk 2).
2. **A lane opened at `main.py:391` leaks on a later startup failure today.** C-1 Task 1 fixes it incidentally by moving the close into `lifespan`'s `finally`; say so in the PR body.
3. **`podman volume prune` would delete the fleet registry** while the stack is down (`learn-ai_alpaca-fleet-control` has no attached container). Every cleanup is an explicit `grep '^fleetqualification' | xargs podman volume rm`.
4. **The qualification harness renders `compose config` with the real Live credential file before it fences.** Export the three `FLEET_*_ENV_FILE` variables to an empty file before running it (Lane F R3).
5. **CI proves `docker compose`; the host runs `podman compose`.** The migration runbook's `render_fleet_topology.py --engine "podman compose" --check` step is what closes the gap; do not skip it.
6. **`FLEET_COORDINATOR_SERVICE_TOKEN` misconfigured on a lane turns every forwarded command into a 400** once D-C lands. Check `GET /api/broker-clerks` shows both lanes ready before issuing a command after that restart.
7. **`Frontend/src/assets/docs/ibkr-setup-guide.md` still tells operators to set `IBKR_READONLY set to false`** for order-capable testing. Actuation was retired; the served copy escapes the docs contract. B-B fixes it.
8. **Two memory notes were stale** (`reference_ibkr_broker_enabled_is_load_bearing_for_alpaca`, `project_ibkr_gateway_nightly_blackout`). Both were corrected on 2026-09-14.

---

## Self-review

- **Spec coverage:** every one of the 18 issues maps to a lane task or a recorded decision (table above). #2072 and the #2077 rename are decisions with a named first slice, deliberately unscheduled.
- **Placeholders:** none in this master; lane files carry the code. Lane E Task 11 is conditional on decision 13 and stands as written if flipped.
- **Type consistency:** names that cross lanes are pinned here: `mutations_closed` (C), `laneFence` / `refresh()` (E), `FLEET_REFUSAL_REASONS` (E-B), `compose.fleet.dev.yaml` and `scripts/render_fleet_topology.py` (F), `_alpha_canonical_account_id` / `_beta_canonical_account_id` (G), `unreachable_since_ms` (B), `TradeUpdateCounters.connects` (A).
