# Autonomous run prompt — Bot Cockpit deep functional exploration and remediation

**Scheduled fire:** 2026-06-22 02:05 America/Chicago (local launchd on inkant's Mac).
**Wall-clock cap:** 6 hours. Hard stop at 08:05 CT.
**Working directory:** `/Users/inkant/learn-ai`
**Starting point:** `master` at `77bf6563` (619-D5).
**Branch to create:** `auto/bot-cockpit-remediation-2026-06-22`.

You are a Claude Code agent running unattended on a production-critical delivery. The user is asleep. Do not stop for ambiguity — decide, document the decision in `docs/audits/bot-cockpit/decisions-for-review.md`, keep going. Stop only for safety preflight failure, circuit-breaker trip, or wall-clock cap.

---

## 1. Mission

Prove that the Bot Cockpit tells the truth about server state and that every operator affordance behaves exactly as advertised. The Python `operator_surface` projection and shared server-side capability resolvers are authoritative for operational verdicts, action eligibility, disabled-reason codes, attention/remediation routing, and all process/broker/readiness/mutation-attempt/risk classifications. Angular may format and present those values; it must not infer operational truth from raw evidence, invent a master bot status, or independently decide whether an action is safe.

This is an implementation task, not a read-only audit. Find bugs, write regression tests, fix them, validate the complete operator workflow, and reconcile the documentation with shipped behavior.

---

## 2. Read first

- `AGENTS.md`
- `CONTEXT.md`
- `CLAUDE.md` and `.claude/CLAUDE.md`
- `.claude/skills/auto-research-tick/SKILL.md` (methodology only; do not inherit its read-only restrictions and do not modify its `state.json`)
- `.claude/rules/angular.md`
- `.claude/rules/python.md`
- `.claude/rules/dotnet.md`
- `.claude/rules/testing.md`
- `.claude/rules/numerical-rigor.md`
- `docs/architecture/adrs/0004-instance-addressed-operator-control-plane.md`
- `docs/architecture/adrs/0010-operator-action-contract-flatten-pause-stop.md` (including the 619-D amendment)
- `docs/architecture/adrs/0013-operator-surface-judgment-vs-evidence.md`
- `docs/runbooks/broker-instance-operator-surface.md`
- `docs/operator-architecture-and-runbook.md`

The existing canonical operator manual is `docs/operator-architecture-and-runbook.md`. Update it; do not create a competing manual. If genuinely missing in the checked-out revision, create it. Also update the cockpit-specific runbook.

**ADR rule (hard):** You do not edit any file under `docs/architecture/adrs/`. Period. Not to "amend", "clarify", or "add a note". Every ADR conflict you encounter goes into `docs/audits/bot-cockpit/decisions-for-review.md` with citations, observed behavior, proposed amendment, and severity. The operator runbook and cockpit runbook are NOT ADRs — edit those freely.

---

## 3. Execution envelope

### 3.1 Phasing (within the 6h cap)

- **T+0:00 → T+0:15** — Preflight (§4), baseline capture (§5), branch creation, audit workspace scaffolding.
- **T+0:15 → T+4:00** — Audit, write tests, fix P0/P1.
- **T+4:00 → T+5:00** — P2 fixes, doc reconciliation, `thermo-nuclear-code-quality-review`.
- **T+5:00 → T+5:30** — Push branch, open PR.
- **T+5:30 → T+6:00** — CI watch; auto-merge if all gates green, else leave draft.
- **T+6:00** — Hard stop. No new work. Whatever is committed is committed. If not yet pushed, push current branch and leave draft PR.

### 3.2 Graceful degradation under time pressure

At T+4h, triage remaining work. Drop anything not on the P0/P1 critical path. Stop opening new investigation threads. At T+6h hard cap: commit current state, push, open draft PR (regardless of completeness) with a clearly labeled `STATUS: INTERRUPTED AT WALL-CLOCK CAP` section listing what was completed, what was in flight, what was never started. **Interrupted runs never auto-merge**, even if gates happen to be green.

Priority order for degradation:
1. Audit inventory + matrix (durable artifact under `docs/audits/bot-cockpit/`) — always produced.
2. P0 findings — discovered, regression-tested, fixed, verified. Never deferred.
3. P1 findings — same.
4. P2 findings — fix if cheap; document and defer if not.
5. P3 findings — record only.
6. Doc reconciliation — required for auto-merge, not for ship-the-PR.
7. Post-fix browser evidence for affordances actually fixed.

### 3.3 Git authority

- Commit locally on `auto/bot-cockpit-remediation-2026-06-22` — yes.
- Push the branch — yes.
- Open a PR — yes. Open as **draft** initially; convert to ready-for-review at the moment auto-merge is attempted.
- **Squash-merge** the PR — yes, **if and only if** every gate in §3.4 is green. PR title is the squash subject.
- Force-push to feature branch — yes (especially for rebasing onto fresher master). Never force-push to `master`.
- Never use `--no-verify`, `--no-gpg-sign`, or any hook bypass.
- If `master` advances during the run, rebase the feature branch onto fresh master. Do not merge master in with a merge commit.

Commit subject style: `cockpit-audit — <one-line scope>` matching the repo's recent history. Commit frequently in logical chunks (one commit per finding fixed, one for tests, one for doc reconcile, etc.).

### 3.4 Merge gates — all must hold simultaneously before auto-merge

1. CI green on the PR (every check `completed` + `success`).
2. Project-scope lint green locally before push:
   - `ruff check PythonDataService/app/ PythonDataService/tests/`
   - `npx eslint Frontend/src/ --max-warnings 0`
   - `dotnet format podman.sln --verify-no-changes`
3. Project-scope tests green locally before push (excluding inherited baseline failures from §5):
   - `podman exec polygon-data-service python -m pytest /app/tests`
   - `cd Backend.Tests && dotnet test`
   - `podman exec my-frontend npx ng test --watch=false`
4. `thermo-nuclear-code-quality-review` skill run before the first push; every major finding addressed in-branch or explicitly documented in the PR body with the reason. Re-pushes after PR review do NOT re-trigger thermo.
5. Zero P0 and zero P1 findings open.
6. Browser evidence (post-fix screenshots at 1440×1000) captured for every affordance that was fixed.
7. Docs reconciled: `docs/operator-architecture-and-runbook.md` + `docs/runbooks/broker-instance-operator-surface.md` updated.
8. PR body contains the full final report (§9).

If any gate fails: push the branch, leave PR as **draft**, do not merge, surface the blocker in the PR description and in `RUN-SUMMARY-2026-06-22.md`.

### 3.5 Ambiguity protocol — no blockers

You do not stop for ambiguity. When you have to pick between defensible options:
1. Pick the most defensible option using the authority hierarchy from `CLAUDE.md` (vendored references → official docs → `.claude/rules/*` → model knowledge).
2. Document the decision in `docs/audits/bot-cockpit/decisions-for-review.md` with: the choice made, alternatives considered, citations, file references, and the test that would change with each branch.
3. Proceed.

`decisions-for-review.md` covers ADR conflicts, invented operator copy strings, behavioral changes shipped, tests skipped, schema changes, and anything else picked under uncertainty. **An empty decisions-for-review.md is not required for auto-merge.** Auto-merge proceeds when §3.4 gates are green regardless; the user reads `decisions-for-review.md` in the morning as a parallel artifact.

---

## 4. Safety preflight (runs once at T+0, before any work)

The IBKR account plugged in is paper-only; nothing is at stake monetarily. Preflight is a misconfiguration-detection sanity check, not a money-risk gate.

1. `curl http://localhost:8000/health` and the broker safety-verdict endpoint. The current closed enum is {`PAPER_ONLY`, `UNSAFE`, `UNKNOWN`}; the audit may proceed only on `PAPER_ONLY`. If the verdict is `UNSAFE` or `UNKNOWN`, or returns any other value: abort the run (write `docs/audits/bot-cockpit/run-aborted-2026-06-22.md` with reason, write `RUN-SUMMARY-2026-06-22.md` with `STATUS: ABORTED`, exit cleanly). Fail-closed: an unknown verdict aborts.
2. Inspect env for `IBKR_LIVE_TRADING_OPT_IN=true` / `LIVE_TRADING=1` / equivalent. If any are set: abort.
3. `podman compose ps` — confirm all 5 services (`my-frontend`, `my-backend`, `polygon-data-service`, `my-postgres`, `my-redis`) are `Up`. If not, attempt `./restart.sh` once; if still not all `Up` after 5 min, abort. Backend listens on **port 5050** (not 5000), frontend on 4200, python on 8000, postgres on 5432, redis on 6379.
4. List existing broker instances; record their IDs. Any instance with `live_binding_present=true` against a non-paper account: abort.
5. Confirm at least one paper instance exists, or create one via the deploy-form path with ID prefix `audit-2026-06-22-`.

### Destructive-mutation rules (apply throughout the run)

- Destructive mutations (`Resume`, `Pause`, `Stop`, `Flatten-and-pause`, `Mark-poisoned`, `Reconcile`) run only against instances whose IDs start with `audit-2026-06-22-` that you created, OR existing instances whose runtime snapshot reports `safety_verdict=PAPER_ONLY` AND `live_binding_present=false` AND no real positions.
- Re-check the safety verdict on the target instance within 5 seconds of every destructive click. If it changed, abort that mutation, log it, do not retry.
- Never mutate an instance with owned positions you did not create yourself.
- Post-run cleanup: every `audit-2026-06-22-*` instance you created gets flattened, stopped, and marked for teardown before PR open. Cleanup failure = blocker for auto-merge.

---

## 5. Inherited-failure baseline (T+0:00 → T+0:15, before any edits)

On a clean `master` checkout (you are starting from `77bf6563`):
1. Run all three project-scope test suites + all three linters + Playwright.
2. Dump the full pass/fail set with timestamps to `docs/audits/bot-cockpit/baseline-2026-06-22.json`.

Every post-edit test failure is compared against this baseline:
- Already-red in baseline = inherited; logged, ignored for gating.
- Was-green in baseline, now red = your regression. Fix or revert before push.
- Was-red in baseline, now green = incidental fix. Record; don't celebrate.

---

## 6. CI monitoring + hard-fail circuit breakers

### 6.1 CI watch (after push + PR open)

Use `gh pr checks <pr-number> --watch` (or poll `gh pr checks --json conclusion` every 60s). Do not merge until all checks report `completed` + `conclusion=success`. If any check times out, errors, or fails: leave PR draft, write the failing check logs to `decisions-for-review.md`.

### 6.2 Circuit breakers — any one trips → immediate abort

On trip: commit current work, push branch, leave PR draft, write `decisions-for-review.md` with reason, write `RUN-SUMMARY-2026-06-22.md` with `STATUS: ABORTED — <reason>`, exit cleanly. Do **not** retry, do **not** escalate to "make a different decision."

1. **Stack dies and doesn't recover.** Any of the 5 services not `Up` for >2 min. Attempt `./restart.sh` once. If still unhealthy 5 min after restart: abort.
2. **Disk pressure.** `df -h /Users/inkant` >95% used. Attempt to free space (delete intermediate logs >1h old); if still >95%: abort.
3. **Repeated identical failures.** Same test/command failing 3 times with identical output across your "fix" attempts. Abort (you're in a loop).
4. **Master advances >5 commits during the run.** Pause, dump current state, leave PR draft. Do not attempt to rebase past that.
5. **Wall-clock cap T+6h.** Hard stop per §3.1.

---

## 7. Scope

### 7.1 IN scope (you may inventory, audit, test, and modify)

- `Frontend/src/app/components/broker/cockpit-v2/**` (all four tabs)
- `Frontend/src/app/components/broker/broker-deploy-form/**`
- `Frontend/src/app/components/broker/broker-start-stop-card/**`
- Anything cockpit-v2 routes (`/broker/instances`, `/broker/instances/:id`) directly imports — services, models, GraphQL operations, shared capability/copy maps, operator-surface client types
- Server side: `PythonDataService/app/operator_surface/**`, FastAPI routes backing cockpit mutations (Resume / Pause / Stop / Flatten-and-pause / Mark-poisoned / Reconcile), shared capability resolvers, `mutation_attempt` and `broker_observation_consistency` surfaces
- Backend GraphQL resolvers proxying operator-surface fields
- `Frontend/tests/e2e/**` for new Playwright specs
- `PythonDataService/tests/**` for new pytest
- `Backend.Tests/**` for xUnit against affected resolvers
- `docs/operator-architecture-and-runbook.md`, `docs/runbooks/broker-instance-operator-surface.md`, `docs/audits/bot-cockpit/**`

### 7.2 OUT of scope (do not modify; may read for context)

- Any `broker-*` sibling component cockpit-v2 does **not** transitively import.
- Engine math, indicators, strategies, backtesting, options pricing, market-data ingestion — `PythonDataService/app/engine/**`, `references/**`, `tests/fixtures/golden/**`.
- Legacy `cockpit` v1 surfaces — see §7.3.
- Anything in `Frontend/src/app/components/` outside `broker/`.
- Migrations (`Backend/Migrations/**`).
- `.claude/`, `restart.sh`, container Dockerfiles, CI config.
- `docs/architecture/adrs/**` (per §2 ADR rule).
- `docs/audits/auto-research/state.json` (do not touch).

### 7.3 Legacy cockpit handling

You may **delete** legacy v1 cockpit code only if you can prove (via route table + `git grep` + import graph) that nothing live references it. Otherwise list the suspected legacy surface in the audit as a P3 finding and leave it alone. No "I think this is unused" deletes.

### 7.4 Scope-expansion protocol

If fixing a P0/P1 requires editing an out-of-scope file: record the edit and the justification in the PR body under a `## Scope expansions` section. Each expansion called out explicitly. No convenient drive-by cleanup. If the entire fix lives in an out-of-scope module: record it as a finding with a fix proposal in the audit, do not implement, flag in `decisions-for-review.md`.

---

## 8. Coverage requirements

### 8.1 Routes and surfaces to exercise

- `/broker/instances` (list view)
- `/broker/instances/:id` (detail view)
- Full `cockpit-v2` surface and all four tabs
- Bot switching and background attention state
- Configuration and redeploy flows
- Broker deploy form and action-plan configuration
- Resume, Pause, Stop, Flatten-and-pause, Mark-poisoned, Reconcile
- Mutation uncertainty and the 619-D recovery workflow
- Host-process notices and copyable commands
- Broker connection versus broker safety
- Broker-observation consistency
- Runtime/control-plane freshness
- Readiness gates and suggested actions
- Current risk, positions, pending orders, order cap, incidents, trades, audit/provenance, sizing, configuration

### 8.2 UI states to cover

initial, loading, empty, steady, attention, stale, unreachable, partial-response, error, request-in-flight, success, conflict, unknown-outcome.

### 8.3 Race and lifecycle conditions

refreshes, polling races, stale responses, rapid repeated clicks, route changes, component destruction during a request.

### 8.4 Accessibility

keyboard navigation, focus behavior, accessible names, tooltip accessibility. UI must pass AXE. WCAG AA minimums (focus, contrast, ARIA).

### 8.5 Capability/state matrix

Build from actual server contracts, not by guessing. Cover combinations of:
- intent: `RUNNING`, `PAUSED`, `STOPPED`
- host process: `RUNNING`, `STOPPING`, `EXITED`, `IDLE`, `WAITING_FOR_HOST`, `UNREACHABLE`
- broker safety: `PAPER_ONLY`, `UNSAFE`, `UNKNOWN`
- broker connection: `CONNECTED`, `DISCONNECTED`, `UNKNOWN`
- readiness: `READY`, `DEGRADED`, `BLOCKED`, `UNKNOWN`
- runtime freshness: fresh + each stale/demoted mode
- live binding present/absent
- owned positions present/absent
- poisoned/not poisoned
- mutation attempt: `PREPARED`, `DISPATCHING`, `RESPONSE_CONFIRMED`, `OUTCOME_UNKNOWN`, `EFFECT_CONFIRMED`, `EFFECT_NOT_OBSERVED`, `NOT_PROVABLE`, `EVIDENCE_CONFLICT`
- broker-observation consistency: `CONSISTENT`, `CONFLICTING`, `UNKNOWN`, `NOT_COMPARABLE`
- request idle/in-flight
- success, 409 capability conflict, timeout, transport loss, malformed/partial response

Confirm independent `PROCESS`, `INTENT`, `READINESS`, `BROKER CONNECTION`, and `BROKER SAFETY` values in every end-to-end scenario. **Do not assert or introduce a synthetic "overall bot status."**

---

## 9. Button and action contract

Inventory every clickable or keyboard-activatable affordance in cockpit + configuration/deploy UI: icon buttons, links styled as buttons, tabs, disclosure controls, copy controls, dialog actions, destructive confirmations.

For every affordance:

1. Visible label, accessible name, tooltip/help text must describe its real effect.
2. If disabled: useful operator-language tooltip explaining why.
3. Tooltip discoverable despite native disabled-button event behavior (use accessible wrapper or established pattern).
4. Server-disabled actions use the server-authored structured reason code and shared frontend copy map. Unknown codes must remain visibly diagnosable, not silently rendered as generic success-looking copy.
5. Locally disabled actions (request-in-flight, local transport staleness) honestly explain the transient local reason; do not pretend it came from the server.
6. Enabled activation calls the correct endpoint exactly once with the correct payload.
7. The endpoint re-evaluates eligibility server-side immediately before mutation. Stale UI snapshot must not bypass a safety gate.
8. UI shows honest pending, confirmed, failed, conflict, outcome-unknown states.
9. Never show success merely because the HTTP request was sent. Success requires the contractually appropriate response or observed effect.
10. Destructive actions preserve confirmation, focus, and cancellation behavior.
11. No displayed control may be a placeholder, no-op, dead click target, misleading navigation, or control for functionality the cockpit does not own.
12. UI updates from a fresh server response after mutation. Does not fabricate the resulting state.

---

## 10. Finding taxonomy

- **P0:** unsafe/unintended broker-side action, conceals live exposure, reports confirmed safety/action when not proven.
- **P1:** enabled action performs wrong operation, server/UI eligibility mismatch, misleading success, missing critical action, material state dishonesty.
- **P2:** missing disabled tooltip, stale UI after action, inaccessible control, confusing operator copy, incomplete error handling, documentation contradiction.
- **P3:** cosmetic or low-impact consistency.

Each finding records: route + UI state, exact control, expected behavior, observed behavior, server payload/response, source files, reproduction steps, screenshot where useful, regression test, fix, verification result.

For every reproducible in-scope defect:
1. Add a regression test that fails before the fix.
2. Implement the smallest coherent fix at the authoritative layer.
3. Re-run targeted tests.
4. Run the relevant project-level frontend and Python suites before completion.
5. Run lint/type checking at project scope.
6. Re-run the browser workflow and capture post-fix evidence.

Do not add dependencies unless unavoidable. If one is needed, document the alternative considered and why rejected.

---

## 11. Documentation reconciliation (after fixes land)

- Update `docs/operator-architecture-and-runbook.md` as the canonical operator manual.
- Update `docs/runbooks/broker-instance-operator-surface.md`.
- Remove or clearly label stale pre-619 behavior (old action semantics, obsolete Reconcile/no-op descriptions).
- Document every cockpit action: eligibility, effect, disabled reasons, confirmation, pending state, success result, failure result, outcome-unknown behavior, operator recovery.
- Document configuration/redeploy behavior + difference between redeploy, restart, Resume, host-process ownership.
- Document 619-D mutation attempts, conflict gating, effect-based Reconcile, broker-observation consistency, terminal results.
- Include an operator troubleshooting table keyed by visible UI message/reason code.
- Screenshots and labels match final UI.
- Identify deprecated HTML/PDF manuals explicitly if they remain in the tree; do not let them present contradictory instructions as current.

---

## 12. Audit workspace artifacts

All under `docs/audits/bot-cockpit/`:

- `charter.md` — what's being audited, scope, methodology, exit criteria.
- `affordance-inventory.md` — every clickable/keyboard-activatable surface, label, accessible name, server reason code, observed behavior.
- `state-matrix.md` — capability/state combinations covered (§8.5).
- `findings/` — one markdown file per finding, named `P{0|1|2|3}-NNN-<slug>.md`.
- `evidence/screenshots/` — pre- and post-fix screenshots at 1440×1000.
- `baseline-2026-06-22.json` — inherited-failure baseline from §5.
- `decisions-for-review.md` — every uncertain decision (§3.5).
- `run-2026-06-22.log` — full stdout/stderr tee'd from the wrapper.
- `RUN-SUMMARY-2026-06-22.md` — single-page morning landing pad (§13).

---

## 13. Morning landing pad — `RUN-SUMMARY-2026-06-22.md`

Always written at run completion or abort. First line is exactly one of:
- `STATUS: AUTO-MERGED ✓`
- `STATUS: DRAFT PR OPEN — REVIEW NEEDED`
- `STATUS: ABORTED — <reason>`

Body includes:
- PR URL + branch name + commit count
- P0/P1/P2/P3 finding counts (opened, fixed, deferred)
- Affordance coverage count
- State-matrix coverage
- Lint/test command exit codes
- Paths to: `decisions-for-review.md`, `affordance-inventory.md`, `state-matrix.md`, evidence dir, `run-2026-06-22.log`
- Any contract conflicts requiring explicit human decision
- Tests and commands run
- Documentation updated

Also: send a macOS notification at completion via `osascript -e 'display notification "<STATUS line>" with title "Bot Cockpit Audit"'`.

---

## 14. Execution mechanics

- Browser automation: use existing Playwright at `Frontend/playwright.config.ts` + `Frontend/tests/e2e`. Do not install replacement tooling.
- Screenshots: 1440×1000 desktop for representative states and all failures.
- Trace each action from Angular → API endpoint → shared capability resolver → durable state/artifact write → refreshed `operator_surface`.
- Compare frontend types against the FastAPI/OpenAPI response contract.
- Search for duplicated capability logic or frontend-derived verdicts.
- Search for dead, redundant, legacy cockpit surfaces.
- Test direct endpoint calls to prove server enforcement independently of button state.
- Test stale-status races where eligibility changes between rendering and clicking.
- Test mutation uncertainty and Reconcile without automatically replaying the original mutation.
- Confirm timestamps remain `int64 ms UTC` at all boundaries.

---

## 15. Final report (in PR body and `RUN-SUMMARY-2026-06-22.md`)

- Executive verdict
- Commit/branch tested
- Affordance coverage count
- State-matrix coverage
- Findings opened and fixed by severity
- Remaining risks or blocked checks
- Tests and commands run
- Screenshot/report paths
- Documentation updated
- Any contract conflicts requiring explicit human decision

---

## 16. Completion criteria

Continue until all of these hold (or wall-clock cap):

- Every cockpit/configuration affordance inventoried.
- Every disabled affordance has an accessible, honest explanation.
- Every enabled affordance performs its stated operation exactly once.
- Mutations are re-gated server-side.
- Pending/success/failure/unknown states are honest.
- The UI refreshes to server truth.
- No P0/P1 findings open.
- P2 findings fixed or explicitly documented with defensible blocker.
- Targeted and project-level checks pass, inherited failures clearly separated.
- Browser evidence confirms corrected workflows.
- Operator manual and cockpit runbook describe shipped behavior without contradictions.

When all hold: convert PR from draft to ready, attempt squash-merge, write `RUN-SUMMARY-2026-06-22.md` with `STATUS: AUTO-MERGED ✓`, send macOS notification, exit.

---

End of prompt.
