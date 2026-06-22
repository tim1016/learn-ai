STATUS: DRAFT PR OPEN — REVIEW NEEDED

# Bot Cockpit deep functional audit — 2026-06-22 morning landing pad

## PR

- **#642** — https://github.com/tim1016/learn-ai/pull/642 (DRAFT)
- Branch: `auto/bot-cockpit-remediation-2026-06-22`
- Commits: 8
- Base: `master @ 77bf6563` (619-D5)

## How this run started

The launchd plist fired at the scheduled 02:05 CT but the wrapper called `/usr/bin/timeout` (a GNU coreutils binary not shipped on macOS — `gtimeout` after `brew install coreutils`). Exit 127 before Claude was invoked; the plist self-unloaded as designed. User asked me to fire the prompt interactively this morning. Full failure record in `run-2026-06-22.log` (committed). Wrapper fix is a follow-up — not in this PR.

## Findings

| ID | Severity | Title | Status |
|---|---|---|---|
| P1-001 | P1 | Env chip is `status() ? 'PAPER' : ''` (synthetic verdict, ADR-0013 §1 violation) | ✓ Fixed + 4 new tests |
| P2-002 | P2 | No Frontend reason-code → operator-language copy map | ✓ Fixed + parity-locked Vitest |
| P3-007 | P3 | Legacy `broker-start-stop-card/` orphaned; deletion authorized by §7.3 proof | ✓ Deleted |
| P3-008 | P3 | `broker-paper-run/` "kept for reference" per route comment | Audit-only — no action |

| Severity | Opened | Fixed | Deferred |
|---|---|---|---|
| P0 | 0 | 0 | 0 |
| P1 | 1 | 1 | 0 |
| P2 | 1 | 1 | 0 |
| P3 | 2 | 1 | 1 (audit-only) |

## Affordance coverage

- 58 files inventoried under `Frontend/src/app/components/broker/cockpit-v2/**`
- Every clickable / keyboard-activatable affordance in the cockpit-v2 surface + `broker-deploy-form/` + (deleted) `broker-start-stop-card/` recorded in `docs/audits/bot-cockpit/affordance-inventory.md` with file:line, label, disabled-reason source, endpoint hit, and triage notes
- High-priority flags surfaced by the inventory agent triaged and routed to the four findings above; lower-conviction or out-of-scope flags recorded in the "Out of audit scope" section of the inventory

## State-matrix coverage

`docs/audits/bot-cockpit/state-matrix.md` records the §8.5 cells pinned by the existing test surface plus the new specs this audit ships. Honest about what is not pinned (no Playwright e2e in this interactive pass; the existing `Frontend/tests/e2e/cockpit-*.spec.ts` suite is unchanged).

## Lint / test results

| Suite | Master | Branch | Verdict |
|---|---|---|---|
| Pytest (`-k 'not slow'`) | 33 failed / 5296 passed | unchanged | ✓ no regression — no Python files touched |
| ng test | 6 failed / 114 passed | 7 failed / 113 passed | ⚠ flaky inherited; rotating subset; **NONE** in `Frontend/src/app/components/broker/**` scope |
| eslint `--max-warnings 0` | 175 warnings | 175 warnings | ✓ no regression |
| ruff `app/ tests/` | 1 error | 1 error | ✓ no regression |
| dotnet format `Backend.csproj --verify-no-changes` | clean | clean | ✓ |

In-scope green on every run: **35** disabled-reason-copy specs + **129** cockpit-v2 component specs + **289** broker-namespace specs.

ng-test flaky-failure cluster (all out-of-scope): `portfolio.component.spec`, `strategy-builder.component.spec`, `ticker-explorer.component.spec`, `data-lab/past-chain-inspector`, `research-lab/feature-runner`, `research-lab/strategy-runs/run-detail-page`, `research-lab/strategy-runs/run-detail-page/baselines-section`, `indicator-picker.component.spec`, `lean-engine/engine-lab-run-history`, `dashboard.component.spec`. "Hook timed out in 10000ms" matches several. These tests were last modified 100+ commits ago; not introduced by this audit.

## Thermo-nuclear review

PASS. Report at `thermo-report-2026-06-22.md`. No major findings. Two minor non-blocking observations:
- envChip switch could carry exhaustiveness check (closed 3-value enum; zero runtime risk today)
- cockpit-shell.component.ts is 671 lines (well under 1000; worth tracking)

## Why I did NOT auto-merge

The run-prompt §3.4 gate-3 says project-scope tests must be green excluding inherited baseline failures. ng-test on master + on this branch each fail a rotating subset of out-of-scope flaky tests; the cockpit-v2 surface is itself green on every run. Per the conservative D-003 decision logged at session start ("if any gate is anything other than fully green … I leave the PR as draft"), this PR opens as draft. The user reviews and merges manually after confirming the flaky failures are inherited (master comparison data is recorded in `baseline-2026-06-22.json` for that confirmation).

## Documentation updated

- `docs/operator-architecture-and-runbook.md`:
  - §7 pre-deploy checklist item #6 (was "hero band reads Paper trading mode") rewritten to describe the env chip + identity-strip SAFETY indicator and the P1-001 invariant
  - §9.5 cockpit-safety-unknown troubleshooting reflects the ADR-0011 amendment
  - New §9.6 — troubleshooting `Unrecognized reason code:` tooltips + the workflow for adding codes to the parity-locked map
- `docs/runbooks/broker-instance-operator-surface.md`:
  - "Reason-code vocabulary (closed, updated 2026-06-22)" — points at the actual server source-of-truth + the new copy map module; removes a dead `action-reason-codes.ts` reference
  - "deleted `<app-broker-start-stop-card>`" → accurate historical citation
  - Quick-audit checklist item #1 rewritten to specify both render sites + the structural invariant

## ADR conflicts requiring human review

None. The audit reads ADR-0004, ADR-0010, ADR-0013 as binding and authors no amendments to any ADR.

## Tests / commands run

```
podman exec polygon-data-service python -m pytest /app/tests --ignore=... -k 'not slow'
podman exec my-frontend npx eslint /app/src/ --max-warnings 0
podman exec my-frontend npx ng test --watch=false
podman exec my-backend dotnet format /src/Backend.csproj --verify-no-changes
podman exec polygon-data-service ruff check /app/app/ /app/tests/
```

## Paths to artifacts

- Decisions log: `docs/audits/bot-cockpit/decisions-for-review.md`
- Inventory: `docs/audits/bot-cockpit/affordance-inventory.md`
- State matrix: `docs/audits/bot-cockpit/state-matrix.md`
- Charter: `docs/audits/bot-cockpit/charter.md`
- Baseline (incl. lint + pytest + ng-test): `docs/audits/bot-cockpit/baseline-2026-06-22.json`
- Findings: `docs/audits/bot-cockpit/findings/`
- Thermo report: `docs/audits/bot-cockpit/thermo-report-2026-06-22.md`
- Original overnight prompt + wrapper + log: `docs/audits/bot-cockpit/{run-prompt-2026-06-22.md, run-2026-06-22.sh, run-2026-06-22.log, launchd-*.log}`
