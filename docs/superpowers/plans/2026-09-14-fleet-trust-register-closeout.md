# Fleet Trust Register — close-out plan for the 10 remaining issues

> Successor to `2026-09-14-fleet-trust-register-fixes.md` (master). That document planned
> 18 issues across seven lanes; 8 are closed. This one sequences what is left.

**Written:** 2026-09-14 12:23 CDT, against `origin/master` `cec92fcf`.

## 0. State verified at writing time

| Fact | Value |
|---|---|
| `origin/master` | `cec92fcf` (= main checkout HEAD) |
| **Deployed** | **YES** — all seven containers restarted 12:17 CDT on `cec92fcf`. Everything merged today is running. |
| Open PRs | none |
| Open issues | 10 — #2066, #2067, #2068, #2069, #2070, #2072, #2074, #2075, #2076, #2080 |
| Closed today | #2063, #2064, #2065, #2071, #2073, #2077, #2079, #2082 |
| Merged today | #2078, #2081, #2083–#2092 (12 PRs) |
| Market | Monday, RTH open. No bot running. Next no-bot window: after 15:00 CT today, or the weekend. |

**This corrects the standing memory note**, which recorded the work as merged-but-not-deployed.
It is deployed. No restart is owed for anything already merged.

## 1. What actually closes each open issue

| Issue | Remaining work | Lane / PR | Blocked by |
|---|---|---|---|
| **#2074** test integrity | Tasks 9, 10, 11 + 5 carried minors | G-3b | nothing — **ready now** |
| **#2069** unscoped mutations | delete 3 dead bot mutations + absence contract + route-inventory doc | D-B (Task 2) | contract chain |
| **#2075** CONTEXT.md false | refuse unpinned on `clerk_agent`, then the CONTEXT.md correction | D-C (Tasks 4, 3) | nothing |
| **#2076** minor cleanups | 7 backend cleanups **and** the frontend half | D-D (Task 5) + Lane E | contract chain / Lane E plan |
| **#2068** fence re-read | freeze-at-open `laneFence`, 3 surfaces, conflict on drift | E-A | **Lane E plan missing** |
| **#2067** refusal rendering | vocabulary snapshot, `next_step` backfill, bare-detail wrapping, frontend copy map | E-B + E-C | **Lane E plan missing** |
| **#2066** topology gitignored | commit `compose.fleet.dev.yaml`, secret-free render gate, `restart.sh -f` | F-1 (Tasks 1–4) | nothing |
| | then the host migration M1–M9 | Wave 5 | **owner**, ~45 min, no bot |
| **#2070** fault matrix unrun | the one-time host qualification run | F Task 7 | **owner only**, ~30 min |
| **#2072** audit read surface | write the Option-2 slice — one coordinator GET | unscheduled | decision 8 |
| **#2080** IBKR reconnect churn | **enable IB Gateway auto-restart** | — | **owner only** |

Two issues need no code from me at all: **#2080** (the log-budget half shipped in #2089; the
remaining fix is a checkbox in the IB Gateway UI) and **#2070** (the nightly CI job shipped in
#2088; what is missing is one host run).

## 2. The gap: Lane E was never planned — CLOSED 2026-09-14

**Resolved.** `2026-09-14-fleet-lane-e-frontend-fence-and-refusals.md` now exists (11 tasks +
1 deferred). It corrected the issue text, the master plan, **and this document** — see §7. The
section below is kept as the record of why the pass was commissioned.

---

### Original finding

The master plan's lane table names `2026-09-14-fleet-lane-e-frontend-fence-and-refusals.md`.
**That file does not exist** — not in the repo, not in any worktree, not in any git object on any
branch. Six of the seven lanes got an Opus planning pass with verified `file:line` claims and a
failing test per task. Lane E did not.

Lane E owns **#2068, #2067 and the frontend half of #2076** — four PRs (E-A through E-D) and
twelve tasks, the largest remaining block. The master plan has the *shape* (PR boundaries,
ordering, decisions 9/10/13) but no code-level detail, and #2068's own correction note says
freeze-at-open is a **correctness** requirement whose bug is dormant only because `refresh()` has
no caller — exactly the kind of claim that needs verification before anyone writes the fix.

**Recommendation: one Opus planning pass for Lane E before executing any of it.**

## 3. Sequencing

Three tracks run in parallel; the contract chain inside Track C is strictly sequential because
each member regenerates `contracts/openapi/python-data-service.openapi.json` and
`Frontend/src/app/api/broker.types.ts`.

### Track A — start now, tests only

**G-3b — closes #2074.** Resume `/Users/inkant/learn-ai-worktrees/fleet-lane-g3`.
**Split into two PRs** so review can start on the first while the second is written:
**3b-i** = Tasks 9 + 10 (falsifiability); **3b-ii** = Task 11 (the 16-file rename sweep).
Each opens with its own thermo by a fresh independent reviewer. #2074 closes on 3b-ii.

- The branch's only commit (`759672ce`) is already in master via #2092 → **reset the branch to
  `origin/master` `cec92fcf`** before starting.
- Ledger: `.superpowers/sdd/2026-09-14-fleet-lane-g-test-integrity/progress.md`; briefs
  `task-9-brief.md`, `task-10-brief.md`, `task-11-brief.md` exist. T11 needs plan §2 (lines 76–125).
- Preconditions re-verified on `cec92fcf`:
  - **T9** — the `http://127.0.0.1:9` sentinel is live at `test_b_scoped_contracts.py:621` and
    `agent_base_url` at `:254`. The work is real.
  - **T10** — the unfalsifiable env canary is live at `test_secret_absence.py:24,29,31,109`.
  - **T11** — 33 renames/strengthenings across 12 files.
- Carry the 5 minors from PR 3a (attribution text, teardown leak on registration failure,
  `_Lane` transport knowledge, docstring citation, the 1115-line file).
- Target: **342 passed** measured on `cec92fcf`, plus net new. (An earlier draft of this
  document said 321; that was inherited from the lane brief and was stale by four merged PRs.)
- **No deploy.** Tests-only.

**Two ordering couplings, both resolved in G-3b's favour:**

1. `test_b_scoped_contracts.py` — the master plan says land D-C first. G-3a already landed ahead of
   it, D-C is not written yet, and G-3b is ready today. **Land G-3b first; D-C rebases.**
2. T11 item 19 vs D-D Task 5(c) — D-D deletes `production_adapter()`, so Lane G's correction note
   says item 19 should assert *absence*. Asserting absence before D-D lands is a red test.
   **Split item 19:** G-3b ships the rename plus `assert set(production_provider_adapters()) ==
   {"alpaca"}` (true today); D-D flips the `production_adapter` half when it deletes the function.

### Track B — Lane E planning pass (parallel with Track A)

Produce `2026-09-14-fleet-lane-e-frontend-fence-and-refusals.md` to the same standard as the other
six: every `file:line` claim verified against `cec92fcf`, a failing test per task, PR boundaries,
and the risks the issue text missed. Scope = master plan Tasks 1–11 (Task 12 stays deferred).

Honour the corrections already recorded, so they are not re-derived:
- #2068 — freeze-at-open is correctness, not UX. **Never add a `refresh()` caller before the freeze.**
- #2067 — the closed refusal set is **29** codes (22 declared, 3 minted inline, 4 subclasses outside
  `errors.py`); the `next_step` gap is 95 of 184 sites across five zero-coverage families.
- Decision 9 — committed-snapshot parity (the `test_vocabulary_snapshot.py` pattern), **not** OpenAPI.
- Decision 13 — delete the dead `listOrderGroups`; Task 11 only bridges it if the owner flips.

### Track C — the contract chain (sequential, one restart each)

```
D-C (#2075)  →  D-B (#2069)  →  D-D (#2076 backend)  →  E-B (#2067 backend)
             →  E-C (#2067 frontend)  →  E-D (#2076 frontend)  →  #2072 slice
```

- **D-C** (Lane D Tasks 4 then 3) regenerates nothing, so it can start the moment G-3b lands.
  Deploy note from the lane ledger: **verify each lane's coordinator token is provisioned first** —
  the exemption is inert when `FLEET_COORDINATOR_SERVICE_TOKEN` is unset.
- **D-B**, **D-D**, **E-B**, **E-D** each regenerate both contracts. Never two in flight unrebased.
- **E-B** must land after D-D (`routing.py` / `errors.py` raise sites) and before E-C (which imports
  E-B's snapshot).
- **#2072** goes last so it rebases once. Option 2 from Lane F §6: one secret-gated coordinator
  `GET /api/broker-clerks/audit/routing-receipts?since_ms=`, reusing
  `require_data_plane_control_secret_always` and `store.list_routing_receipts`; one store change
  (a `since_ms: int` parameter). All times `int64 ms UTC`, per `.claude/rules/temporal-rigor.md`.

### Track D — Lane F (parallel with everything; no contract regen)

**F-1 — Tasks 1–4** (`.gitignore` widen + secret-free compose contract, `compose.fleet.dev.yaml`,
render snapshot + CI gate, `restart.sh -f`). Both of its blockers (#2081, #2078) are merged.
No deploy until the migration.

### Track E — owner only, no code

| Action | Issue | Window | Notes |
|---|---|---|---|
| Enable IB Gateway auto-restart (Configure → Settings → Lock and Exit → Auto restart) | **#2080** | any time | The only remaining work on #2080. Nothing in code can do it. A gateway logged out at the first RTH trigger finalises a running bot as `FEED_DEATH`. |
| One-time fault-matrix qualification run (Lane F Task 7) with the three `FLEET_*_ENV_FILE` vars pointing at an empty file | **#2070** | ~30 min, outside market hours | Precondition for Paper activation, not for the next Live launch. Record evidence in the authority doc §4/§6. |
| Copy `compose.override.yaml` to a 0600 file **outside the repo** | #2066 | before F-1's migration | It is currently the only durable copy of five secrets. |
| Topology migration M1–M9 | **#2066** | ~45 min, no bot, after F-1 | Verify SAME volume names and SAME markers. `compose.fleet.yaml` renames the project and every custody volume — deploying *that* file would orphan Live custody. |

## 4. Deploy windows

Every `./restart.sh` is a `podman compose down`: both clerks drop. Restarts are owed by D-B, D-C,
D-D and E-B; E-A, E-C and E-D need a frontend rebuild; G-3b, F-1 and the Lane E plan need nothing.

Windows: after 15:00 CT on a trading day, before the first launch, or a weekend. Today is Monday
with RTH open — the first window is 15:00 CT today. Batch the Track C restarts rather than
restarting per PR.

## 5. Owner decisions

1. **Write the missing Lane E plan?** Default: yes, one Opus planning pass before any E code.
2. **G-3b before D-C** (deviating from the master's ordering rule)? Default: yes.
3. **Split T11 item 19** so G-3b does not assert the absence of a function D-D has not deleted yet?
   Default: yes.
4. **Build the #2072 read route this round**, or leave it filed? Default per master decision 8:
   build it, last in the chain.

## 6. Delivery protocol (unchanged, per PR)

Branch → regression test verified failing on master → `ruff check PythonDataService/app/
PythonDataService/tests/` → targeted tests plus every suite consuming a shared helper you edited →
**thermo-nuclear review once**, independent subagent, every major finding fixed → push → wait
30 minutes for CI and CodeRabbit → fix what arrived → merge. Re-pushes do not re-trigger thermo.

Constraints that bind every task: never switch branches in `/Users/inkant/learn-ai` (both clerks
bind-mount it); never disable `IBKR_BROKER_ENABLED`; `tests/broker/fleet` binds sockets and FAILS
under the Bash sandbox; frontend pushes need a **full** `ng test`, never only a scoped run; docs
edits must pass `pytest tests/contracts`; plan docs that spell the IBKR read-only env var out as a
key=value assignment fail `tests/contracts` — describe it in prose instead.

---

## 7. Corrections this document inherited and has now fixed

The Lane E planning pass (§2) re-measured everything this plan asserted. Three things were wrong
here and are corrected above:

| Was | Is |
|---|---|
| Track A target "321 passed" | **342 passed** on `cec92fcf` — the figure came from the Lane G brief and was stale by four merged PRs |
| Lane E "12 tasks" | **11 tasks + 1 deferred** |
| Master's "#2067 is 29 codes, 95 of 184 sites, five families" (repeated here) | **30 codes, 97 of 187 sites, six families**; 3 subclasses outside `errors.py`, not 4 |

**And one correction that changes what gets built:** #2068 states the binding-generation fence
"fails closed, but silently". **It fails OPEN.** A cold or failed directory yields a null
generation, `commandContextOf` then omits `expected_effective_binding_generation` entirely, and
both backend checks are `is not None`-gated — so the command routes with **no fence at all**.
Freezing the generation at open does not close that hole; it only names the state.

### Decision 15 — new, and the owner's to make

**When a command has no enforceable fence (cold or failed directory), should the client refuse it,
or dispatch it unfenced as today?** Lane E Task 2 ships `laneFenceIsEnforceable`, which names the
state, but no task in the lane refuses on it: refusing every command while the directory is cold is
a policy change, not a bug fix. **E-A can start either way**, but the answer is needed before Task 2
lands. No default is assumed.

### Two follow-ups Lane E found and deliberately did not take

1. **Four hand-built refusal writers bypass `detail()`** (one builds JSON by string concatenation).
   They sit on raw-ASGI middleware paths with no `Response` object. The vocabulary snapshot will
   pin the *code* but not the *body*, so it implies a guarantee it does not make. File it.
2. **Four command components take their target as an `input()`** from
   `alpaca-desk-account-data.service.ts`, so the freeze belongs at the desk, not in them. Task 6's
   contract spec cannot see them (they contain no `fleetDirectory.lane(` call), so the gap is
   invisible unless written down. File it.

### Inherited test failures to expect

`tests/broker/v2panel` has **5 pre-existing failures** on the host venv — all
`CatalogUnavailableError: POSTGRES_URL is empty`. Environment-only, not Lane E's. Baseline before
treating any red in that directory as yours, and surface it in the PR body.
