# Bot Cockpit deep functional audit + remediation — charter

**Run start:** 2026-06-22 (interactive session, originally scheduled for unattended overnight at 02:05 CT — wrapper failed with `/usr/bin/timeout: No such file or directory` because macOS ships only BSD `timeout` via coreutils).
**Working dir:** `/Users/inkant/learn-ai`
**Starting commit:** `77bf6563` (619-D5 — ADR-0010 amendment + operator runbooks + simulated chaos sweep).
**Branch:** `auto/bot-cockpit-remediation-2026-06-22`.
**Driving prompt:** `docs/audits/bot-cockpit/run-prompt-2026-06-22.md`.

## Mission (verbatim from the prompt)

Prove that the Bot Cockpit tells the truth about server state and that every operator affordance behaves exactly as advertised. The Python `operator_surface` projection and shared server-side capability resolvers are authoritative for operational verdicts, action eligibility, disabled-reason codes, attention/remediation routing, and all process/broker/readiness/mutation-attempt/risk classifications. Angular may format and present those values; it must not infer operational truth from raw evidence, invent a master bot status, or independently decide whether an action is safe.

This is an implementation task, not a read-only audit. Find bugs, write regression tests, fix them, validate the complete operator workflow, and reconcile the documentation with shipped behavior.

## In scope (§7.1)

- `Frontend/src/app/components/broker/cockpit-v2/**`
- `Frontend/src/app/components/broker/broker-deploy-form/**`
- `Frontend/src/app/components/broker/broker-start-stop-card/**`
- Anything cockpit-v2 transitively imports
- `PythonDataService/app/services/operator_surface.py` and shared capability resolvers
- `PythonDataService/app/services/operator_capability.py`, `resume_guard_state.py`, `mutation_attempt.py`, `broker_observation_consistency.py`, `runtime_freshness.py`
- FastAPI routes backing cockpit mutations (Resume / Pause / Stop / Flatten-and-pause / Mark-poisoned / Reconcile)
- Backend GraphQL resolvers proxying operator-surface fields
- `Frontend/tests/e2e/**` for new Playwright specs
- `PythonDataService/tests/**` for new pytest
- `Backend.Tests/**` for xUnit
- `docs/operator-architecture-and-runbook.md`, `docs/runbooks/broker-instance-operator-surface.md`, `docs/audits/bot-cockpit/**`

## Out of scope (§7.2)

- Any `broker-*` sibling component cockpit-v2 does not transitively import
- Engine math, indicators, strategies, backtesting, options pricing, market-data ingestion
- Legacy `cockpit` v1 (delete only with proven non-reference; otherwise leave as P3 finding)
- Anything in `Frontend/src/app/components/` outside `broker/`
- Migrations, `.claude/`, `restart.sh`, container Dockerfiles, CI config
- `docs/architecture/adrs/**` — every ADR conflict goes into `decisions-for-review.md`
- `docs/audits/auto-research/state.json`

## Methodology

1. **Preflight** — safety verdict, services up, no live-trading env, no live-bound instances.
2. **Baseline** — capture pre-existing lint/test failures so they don't masquerade as regressions.
3. **Inventory** — every clickable/keyboard-activatable affordance in cockpit + deploy-form.
4. **State matrix** — covered combinations from §8.5 of the run prompt.
5. **Dimensional audit (parallel reads)** —
   - **Truth dimension.** Does the cockpit show server-authored verdicts verbatim, or synthesize?
   - **Eligibility dimension.** Does every disabled affordance carry a structured reason? Does every enabled affordance get re-evaluated server-side before the mutation lands?
   - **Mutation-uncertainty dimension.** Does 619-D mutation_attempt + broker_observation_consistency reach the cockpit, and is the recovery workflow real?
   - **Honesty dimension.** Pending / success / failure / unknown — does the UI ever claim success on send instead of on observed effect?
   - **Doc-vs-shipped reconciliation.** Where does the runbook contradict shipped behavior?
6. **Fix** — for every reproducible defect: regression test, smallest fix at the authoritative layer, verify, browser evidence.
7. **Thermo + project-scope gates** — before push, run thermo-nuclear-code-quality-review and the three lint + three test suites at project scope.
8. **Push + PR + CI watch + auto-merge** — convert from draft to ready only when all §3.4 gates hold.

## Exit criteria (§16)

- Every cockpit/configuration affordance inventoried.
- Every disabled affordance has an accessible, honest explanation.
- Every enabled affordance performs its stated operation exactly once.
- Mutations are re-gated server-side.
- Pending/success/failure/unknown states are honest.
- The UI refreshes to server truth.
- No P0/P1 findings open.
- P2 findings fixed or explicitly documented with defensible blocker.
- Targeted and project-level checks pass, inherited failures separated.
- Browser evidence confirms corrected workflows.
- Operator manual and cockpit runbook describe shipped behavior without contradictions.
