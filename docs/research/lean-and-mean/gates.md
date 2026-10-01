# Kill list — CI checks and hooks (#2716)

Research ticket on the lean-and-mean map (#2700). Plan only; nothing is cut here.

- **Read at:** master `87b8e261021ec673c2e7c80448c0973bccd45378` (the map's charting SHA).
- **Branch protection and repo settings:** read through the GitHub API on 2026-09-30.
- **Bar (locked):** a check stays only if it is lint/build/typecheck, runs the surviving tests, or guards money-path, math parity, or an OpenAPI/GraphQL contract. The bar is something a check must have, but having it is not enough: a check that only repeats another check goes as a duplicate.
- **Out of scope:** which suites run on the PR path and which run daily. That belongs to the CI-shape item; see the notes for it at the end.

## Required status checks on master

`Backend Tests` is the only required status check. It comes from ruleset 19185467, "Require Backend Tests on master", which targets `refs/heads/master` and has no bypass actors. Classic branch protection returns 404 ("Branch not protected"). Ruleset 15443104 ("1.0") holds a single `deletion` rule with an empty `include`, so it targets no branch.

**No cut below is a required status check, so no cutting PR needs the owner to change branch protection.**

## Kill list

| # | Item | Kind | Bar clause | Required? | Evidence |
|---|---|---|---|---|---|
| 1 | `ci.yml` job `broker-v2-vocabulary-contract` ("Broker V2 Vocabulary Contract") | CI job, duplicate | none. This is not an OpenAPI or GraphQL contract, and a PR-path test already does its regenerate-and-diff. | no | Each of its three regenerate-and-diff steps (`ci.yml:297-348`) is already a pytest test that rebuilds the snapshot and compares it byte for byte: `tests/broker/v2panel/test_vocabulary_snapshot.py:91` + `:71`, `tests/broker/fleet/test_refusal_vocabulary_snapshot.py:116` + `:123`, `tests/broker/fleet/test_operation_catalog_snapshot.py:95` + `:100`. `tests/broker` is in the PR baseline (`PythonDataService/scripts/run_fast_tests.py:36`). The CI checkout includes `Frontend/`, so the byte-identity tests do not take their skip branch (`test_vocabulary_snapshot.py:86-87`). The job's untracked-file check adds nothing: CI checks out only tracked files, so a missing snapshot already fails `test_snapshot_file_exists` (`:64`). |
| 2 | `ci.yml` job `alpaca-sqlite-qualification-smoke` ("Alpaca SQLite Qualification Smoke") | CI job | none. Half of the job reruns tests the shards already run. The other half is a read-latency budget, which is performance, not a money outcome. | no | Its adversarial list `SMOKE_ADVERSARIAL_TESTS` (`app/broker/alpaca/clerk/sqlite/qualification_performance.py:34`) is all node IDs under `tests/broker/alpaca/…`. All of them are in the PR baseline (`run_fast_tests.py:36`) and none is marked `slow`. The remaining gate is `PERFORMANCE_BUDGETS_MS` (`:29`: account snapshot 100 ms, bot snapshot 75 ms, timeline page 100 ms) at 1 bot × 1,000 rows (`:26`). That is projection-read latency, not an order, custody, flatten or fencing outcome. The job runs at `ci.yml:518-524`. |
| 3 | `ci.yml` job `backend-build` ("Backend Build") | CI job, duplicate | build, but the required `Backend Tests` job already covers it in full | no | `podman.sln` holds just `Backend` and `Backend.Tests`. `backend-test` runs `dotnet test Backend.Tests.csproj --configuration Release` (`ci.yml:253`), which builds both projects in Release through `ProjectReference` (`Backend.Tests/Backend.Tests.csproj:24`). That is the same compile as `ci.yml:218`. |
| 4 | `ci.yml` job `adr-status-guard` ("ADR Status Guard"), with `scripts/check_adr_status.py` and `scripts/test_check_adr_status.py` | CI job, gate script and its test | none. It lints ADR prose metadata, not code, and nothing reads the field it enforces. | no | The script checks the `**Status:**` line form and the `**Vocabulary:**` line (`scripts/check_adr_status.py:1-18`). `git grep 'Status:\*\*' -- '*.py' '*.ts'` finds only the guard and its test. Its only runner is `ci.yml:39-40`. |
| 5 | `ci.yml` job `fleet-tooling-tests` ("Fleet Tooling Tests") | CI job | none once the kit goes. This depends on #2707. | no | The job exists only for `scripts/dev/fleet/` (`ci.yml:55-56`). Only the job itself and old audits and plans name the kit (`docs/audits/bot-fleet-stress-2026-08-26.md`, `docs/audits/read-latency-profile-live-2026-08-31.md`, `docs/superpowers/plans/2026-09-14-fleet-lane-d-routes-and-posture.md`). No runbook, skill or CLAUDE.md links it. Its last change was `5d17be2e` (2026-08-26). If #2707 keeps the kit, keep the job too: it lints the kit and runs the kit's only test. |
| 6 | `scripts/check_documentation_contract.py` | gate script, run by pytest | none. It checks documentation bookkeeping: hand-kept document-class and retired-doc lists, local link resolution, ADR index rows, the version strings in AGENTS.md, and byte-identity of the doc copies the app serves. | no (it runs inside the Python shards) | Its only runner is `PythonDataService/tests/contracts/test_documentation_contract.py:6-7`, which loads it by path. The checks are at `check_documentation_contract.py:149-309`, and the lists at `:18-53`. No live doc links the script. **Cut it last; see hazard 3.** |
| 7 | `.claude/hooks/post-edit-validate.sh` | agent hook | none. It is dead: nothing wires it. | n/a | `.claude/settings.json` has no `hooks` block (whole file, `:1-65`). Commit `4430bc11` (2026-04-20) unwired it. `git grep post-edit-validate` finds nothing, and neither `.claude/settings.local.json` nor `~/.claude/settings.json` mentions it. |
| 8 | Root `prettier` and `prettier-plugin-sql` devDependencies, plus `.prettierrc` | dead tooling config | none. Nothing runs prettier. | n/a | `package.json:5-6`. The lint-staged block (`package.json:11-15`) never calls prettier, and no npm script, CI step or hook does either. `git grep prettier` (lockfiles excluded) finds only `.prettierrc` and `package.json`. This sits in the pre-commit block, so it is listed here; #2707 should skip it. |

## What stays

| Check | Bar clause | Required? | Evidence | Note |
|---|---|---|---|---|
| `Backend Tests` | runs tests | **yes** | `ci.yml:236-260` | The only required check. |
| `Backend Lint` | lint | no | `ci.yml:198-207` (`dotnet format --verify-no-changes`) | |
| `Backend GraphQL Contract` | GraphQL contract | no | `ci.yml:231-234`, which exports the schema and diffs `contracts/graphql/backend.schema.graphql` | |
| `Python Lint` | lint | no | `ci.yml:262-274` (`ruff check app/ tests/`) | |
| `Python Test Shard` ×16, plus the `Python Tests` roll-up | runs tests, OpenAPI contract, math parity | no | OpenAPI `--check` at `ci.yml:387`; baseline plus change-driven tests at `:476-483`; the scientific-proof step at `:485-488`; the roll-up at `:490-498` | The scientific-proof step is the **only PR-path runner** of `tests/fixtures/**` (except the manifest test), `tests/research/parity/test_qc_reconciler.py` and `test_cross_engine_study.py`, because none of them is in the `FAST_TEST_PATHS` list (`run_fast_tests.py:21-55`). |
| `Validate Golden Manifest` | math parity | no | `ci.yml:532-546` runs `tests/fixtures/test_golden_manifest.py`: fixture and attribution hashes, canonical module and callable, and no proof gaps (`test_golden_manifest.py:1-14`) | The only PR-path runner of that file, which `:488` excludes. |
| `Frontend Lint` | lint | no | `ci.yml:129-144` | **Drift:** CI runs `npx eslint src/` without `--max-warnings 0` (`ci.yml:143`), but the npm `lint` script (`Frontend/package.json:15`) and CLAUDE.md's documented lint command both use 0 warnings. The e2e step (`:144`) depends on #2734. |
| `Frontend Type Check` | OpenAPI contract, typecheck | no | `codegen:check` regenerates `src/app/api/broker.types.ts` from the OpenAPI snapshot and diffs it (`ci.yml:160`, `Frontend/package.json:18`); `tsc --noEmit` at `:161` | The e2e `tsc` (`:162`) depends on #2734. |
| `Frontend Test Shard` ×6, plus the `Frontend Tests` roll-up | runs tests | no | `ci.yml:164-196` | |
| `Temporal Authority Guard` | lint (code lint of the temporal ban list) | no | `scripts/check_temporal_authority.py:1-7` checks hard-coded RTH times, `mcal.get_calendar` outside the calendar module, `utcnow`/`now()`, `DateTime.Parse`, scattered `DatePipe`, and string-typed timestamps. The rule it enforces says it is "CI-enforceable with grep" (`.claude/rules/temporal-rigor.md:105`). | Nothing else catches these patterns: ruff selects no `DTZ` rules (`PythonDataService/ruff.toml:8-16`). |
| `Alpaca Onboarding Gates` | lint, runs tests, money-path | no | `ci.yml:58-74`. The onboarding gates refuse empty credential pairs and accounts that are not flat on a real-money path (`scripts/alpaca_onboarding_gates.py:18-35`), and the CLAUDE.md-linked runbook `docs/runbooks/add-an-alpaca-account.md` relies on them. The measurement script grounds `FILL_VISIBILITY_GRACE_MS` (`scripts/measure_fill_to_cash_visibility.py:1-13`). | **Only runner** of `scripts/test_alpaca_onboarding_gates.py` and `scripts/test_measure_fill_to_cash_visibility.py` (#2731). If #2731 cuts both, the job shrinks to its two `ruff` steps. |
| `Fleet Topology Render` | money-path | no | `ci.yml:76-127` re-renders compose into `deploy/fleet/topology.snapshot.json` and pins the service set. The snapshot holds the custody volume names, the coordinator's clerk-dir fence and the read-only IBKR feed on Alpaca lanes (`tests/contracts/test_fleet_topology_snapshot.py:31,50,66`). | The pytest side only *reads* the snapshot, so this job is the only PR-path step that ties the snapshot to the compose files. |
| Daily `Backend Full Suite`, `Python Full Suite` | runs tests | no | `daily-tests.yml:16-120` | |
| Daily `Fleet Compose Qualification` | money-path (custody isolation) | no | `PythonDataService/scripts/run_broker_fleet_compose_qualification.py:1-8` proves distinct volume sources, that the coordinator has no custody root, that Paper can be killed, and that Live stays on its own feed. | It qualifies `compose.fleet.yaml` plus the qualification overlay (`:38-41`), not the dev topology that `restart.sh:66` deploys. If #2707 finds `compose.fleet.yaml` unreached, this job goes with it (dead beats sacred). |
| `frontend-e2e.yml` `cockpit-e2e` (daily) | runs tests, **if #2734 keeps the e2e suite** | no | `frontend-e2e.yml:27-87` | Red on 4 of the last 5 scheduled runs (2026-09-26 to 2026-09-30). |
| CodeRabbit (`.coderabbit.yaml`) | the locked process-gate rule: every PR that is not money-path or math ships on CodeRabbit plus green CI | no (a commit status) | Shows as status `CodeRabbit` on PR heads | Its `path_instructions` (`:26-49`) repeat CLAUDE.md and are already stale: they say "Angular 21" (`:29`), but `Frontend/package.json:32` has `^22.0.0`. Trim them when #2715 rewrites the rules. |
| Secret-scanning push protection (repo setting) | money-path: it blocks a push that carries Alpaca or IBKR credentials | n/a | `security_and_analysis.secret_scanning_push_protection: enabled` | |
| CodeQL default setup (repo setting) | **owner question; see below** | no | Default setup, configured for actions, C#, JS/TS and Python. It runs four `Analyze` jobs plus the `CodeQL` check on every PR. 46 alerts are open. | |
| Pre-commit `.husky/pre-commit` → lint-staged | lint | n/a | `package.json:11-15` (eslint `--fix`, `ruff check --fix`, `dotnet format --include`); `core.hooksPath` is `.husky` | I did not check whether `npx eslint` resolves from the repo root (the root has no eslint). |

## What the cuts orphan

- **Row 1:** no files. The `regenerate_*` scripts stay, because the pytest tests import their `build_snapshot`. These docstrings name the dead job and need rewording: `PythonDataService/app/broker/fleet/refusal_vocabulary.py:20`, `scripts/regenerate_broker_v2_vocabulary_snapshot.py:19`, `scripts/regenerate_fleet_operation_catalog_snapshot.py:27`, `scripts/regenerate_fleet_refusal_vocabulary_snapshot.py:22`, `tests/broker/v2panel/test_vocabulary_snapshot.py:83,94`, `tests/broker/fleet/test_refusal_vocabulary_snapshot.py:7`. ADR 0041 also names the job (`docs/architecture/adrs/0041-generated-operator-button-reference.md:16`, #2712).
- **Row 2:** the `--profile smoke` path in `PythonDataService/scripts/run_alpaca_sqlite_qualification.py:138-142`, plus `SMOKE_ADVERSARIAL_TESTS` and `PROFILE_SCALES["smoke"]` in `qualification_performance.py:26,34`. After the cut, only tests reach them: `tests/scripts/test_run_alpaca_sqlite_qualification.py:89` and `tests/broker/alpaca/clerk/sqlite/test_qualification.py`. The runner itself stays, because `compose.yaml:337` runs its `--synthetic-rehearsal` mode. Docs that name the smoke run: `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md:508`, `docs/references/alpaca-sqlite-clerk-invariant-traceability.md:5`, and the committed reports `docs/audits/alpaca-sqlite-clerk-qualification-smoke.{json,md}`.
- **Row 3:** nothing.
- **Row 4:** both scripts go with the job. ADR 0039 and ADR 0040 lose the gate they ask for (hazard 5). Two docstrings mention the guard and are cosmetic: `scripts/alpaca_onboarding_gates.py:11` and `scripts/dev/fleet/test_fleet_tooling.py:4`.
- **Row 5:** `scripts/dev/fleet/test_fleet_tooling.py`, whose only runner is this job. If #2707 cuts the kit, the whole `scripts/dev/fleet/` directory goes.
- **Row 6:** `PythonDataService/tests/contracts/test_documentation_contract.py` (the whole file loads the script), plus the `EXACT_DOCUMENT_CLASSES` and `RETIRED_DOCUMENTS` lists inside the script.
- **Row 7:** the `.claude/hooks/` directory, which becomes empty (hazard 2).
- **Row 8:** the root `package-lock.json` entries for prettier and its plugin.

## Hazards the cutting PR must carry

1. **Branch protection: no change needed.** None of the cuts is a required check; `Backend Tests` stays.
2. **Deleting the hook breaks the docs contract.** `scripts/check_documentation_contract.py:25` lists `.claude/hooks` in `CLAUDE_COMPATIBILITY_PATHS`, and `:194` fails when that path is missing. Deleting the only file in `.claude/hooks/` empties the directory, and git does not track empty directories. Cut row 6 first or in the same PR, or edit that tuple. `.claude/commands` is listed there too (#2715). `.claude/hooks/` is also write-protected in the agent sandbox, so the owner or an unsandboxed session must delete it.
3. **Cut the docs checker last.** Until the docs kill lists land, it is the only thing that fails when a kept doc links to a cut doc. Cut it in or after the last docs-cutting PR, not before.
4. **Row 1 depends on six tests surviving.** The vocabulary-job cut is safe only while #2723 and #2721 keep the "committed matches fresh" and "byte-identical" tests in all three snapshot test files (row 1 evidence). If either ticket cuts them, nothing guards the Python→Frontend snapshot contract any more.
5. **ADR 0039 asks for this CI gate.** Decision 6 says "a CI grep gate" (`docs/architecture/adrs/0039-adr-status-is-decision-standing.md:106`), consequence 7 says "A CI gate is owed" (`:163`), and ADR 0040 hangs its Vocabulary-line check on that same gate (`0040-…md:125`). Cutting row 4 means #2712 must supersede or amend those clauses in the same pass. The locked gate bar wins, but the ADR text must not keep promising a gate that no longer exists.
6. **Only-runner warnings for the test tickets.**
   - `Alpaca Onboarding Gates` is the only runner of `scripts/test_alpaca_onboarding_gates.py` and `scripts/test_measure_fill_to_cash_visibility.py` (#2731).
   - `Fleet Tooling Tests` is the only runner of `scripts/dev/fleet/test_fleet_tooling.py`, and no test ticket owns that file.
   - The scientific-proof step is the only PR-path runner of `tests/fixtures/**` and the two parity files. `Validate Golden Manifest` is the only PR-path runner of `test_golden_manifest.py`.
   - Cutting the e2e workflow (if #2734 cuts the suite) also takes out `ci.yml:144` and `:162`, plus `Frontend/tests/e2e/tsconfig.json`, the Playwright config and the `e2e` npm script.
7. **A test pins the lint-staged config.** `tests/contracts/test_pre_commit_lint_gate_parity.py:26-50` pins the Python lint-staged command to `ruff check` with no `ruff format`. Any edit to the lint-staged block must keep that, or go through #2730.
8. **The temporal guard's allowlists will go stale.** `scripts/check_temporal_authority.py:50-80` exempts the legacy Frontend portfolio and Data Lab paths. If #2709 cuts those surfaces, delete the matching `Allow` entries.
9. **Kill lists age.** Each cutting PR re-checks its evidence at its own SHA.

## Pointers outside this area

- **#2707 (scripts/compose/deploy):** whether `scripts/dev/fleet/` is still reached (row 5 depends on it); whether `compose.fleet.yaml` and `compose.fleet.qualification.yaml` are reached (the daily qualification depends on it); the smoke-only code left behind by row 2. Row 8 (prettier) is listed here, so skip it.
- **#2730 (contract tests):** `tests/contracts/test_documentation_contract.py` is orphaned by row 6. `test_pre_commit_lint_gate_parity.py` and `test_pytest_configuration.py` pin config.
- **#2731 (script tests):** `scripts/test_check_adr_status.py` goes with row 4, so skip it. The two onboarding and measurement tests: judge them, knowing this job is their only runner. Also `tests/scripts/test_run_alpaca_sqlite_qualification.py:89`, which exercises the smoke profile.
- **#2718 (clerk SQLite f–q):** `test_qualification.py` exercises the smoke profile that row 2 orphans.
- **#2721 and #2723:** keep the six snapshot tests that row 1 relies on, or say so in your kill list so the vocabulary job is reinstated.
- **#2734 (e2e):** the suite decides whether `frontend-e2e.yml` and the two e2e steps in `ci.yml` survive.
- **#2712 (ADRs):** ADR 0039 Decision 6 and consequence 7, ADR 0040 `:125`, ADR 0041 `:16`.
- **#2715 (rules and commands):** `.claude/commands/lint-all.md` and `test-all.md` repeat the CI commands; the CLAUDE.md lint line has drifted from CI (Frontend lint drift above); the stale `.coderabbit.yaml` `path_instructions`.
- **#2711 (one-off docs):** audits and plans that describe the cut jobs, and `docs/audits/alpaca-sqlite-clerk-qualification-smoke.{json,md}`.
- **#2713 and #2714:** the smoke mentions in the recovery runbook (`:508`) and the invariant traceability note (`:5`).

## Owner question (rule level)

**Security scanning has no clause in the gate bar.** CodeQL is static analysis on GitHub's side. It is free (the repo is public), it is not required, and it posts a `CodeQL` check on every PR. It has already driven fixes on money-path code (`py/path-injection`, fixed 2026-09-28). Options:

- (a) Count it as lint and keep it. **Recommended.**
- (b) Turn it off; this is the owner's repo setting.

Push protection is kept under money-path, so it does not need the question.

## Notes for the CI-shape item (not decided here)

- The scientific-proof step and the OpenAPI `--check` run in **all 16** Python shards (`ci.yml:386-387`, `:485-488`), so the same work runs 16 times on every PR.
- Only `Backend Tests` is required. The Python and Frontend roll-ups and the contract jobs are not, so the money-path suite (Python) gates merges by habit, not by rule.
- Daily `Python Full Suite` failed on each of the last 4 completed `daily-tests.yml` runs (2026-09-29 to 2026-09-30), while Backend and Fleet Compose passed. `cockpit-e2e` failed on 4 of the last 5. Nobody is acting on daily failures.
- Frontend lint warnings pass CI (`ci.yml:143`), although the documented gate says 0 warnings.
- The 120-second `timeout --signal=KILL` wrappers are at `ci.yml:40,56,72,74,253,521,546`, and the budget runners are `run_fast_tests.py:20` and `Frontend/scripts/run-test-budget.cjs`.

## Not reviewed

- **User-level Claude config** (`~/.claude` hooks, plugins such as the Codex stop-time review gate, git-guardrail hooks). It is outside the repo. I checked only that `~/.claude/settings.json` does not wire row 7.
- **Running anything.** I executed no check. All evidence comes from reading files at the stated SHA and from the GitHub API (rulesets, check runs on PR #2699's head `725aef40`, run history, code-scanning and security settings).
- **The lint-staged ESLint step.** I did not test whether it works when run from the repo root.
- **The 46 open CodeQL alerts.** I did not triage them.
- **Dependabot.** Security updates are disabled and there is no `dependabot.yml`, so there is nothing to judge.
