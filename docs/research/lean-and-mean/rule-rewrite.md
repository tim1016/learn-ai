# Rule rewrite — the text that puts the locked rules into effect (#2739)

Part of map #2700. Written at `6a4d7d39`. This is a spec for the rule-rewrite handoff, not a cut. Each section names a file and gives either its new text or the edit to make. The row ids (C1, R8, S10…) are from the [rules kill list](https://github.com/tim1016/learn-ai/blob/research/lean-rules/docs/research/lean-and-mean/rules.md). A row cited here is cut as that list says.

## Decisions (owner grilling, 2026-09-30)

These replace or sharpen the map's locked gate rules.

1. **Thermo review scales with size.** Count only lines added or changed in Python code and Frontend logic (TypeScript, not templates or styles). Deletions, docs, generated files and lockfiles don't count.
   - Under 1,000 lines: no reviewer.
   - 1,000 lines: up to 1 reviewer.
   - 3,000 lines: up to 2.
   - 8,000 lines: up to 3.

   A PR that only deletes code gets at most one review, however big it is.
2. **Money-path and math PRs always get at least one review, at any size.** The author decides whether a PR is one.
3. **Regression tests are mandatory only for P0 and P1 bugs.** This replaces "money-path and math fixes".
4. **Rule-file loading.** `angular.md`, `dotnet.md` and `python.md` load only for their folder (`paths:` frontmatter). `testing.md`, `numerical-rigor.md` and `temporal-rigor.md` stay always-on.
5. **One shared rule file.** `AGENTS.md` holds every rule Claude and Codex share. `CLAUDE.md` imports it with `@AGENTS.md` and adds only Claude-only lines.
6. **The repo copies of the shared skills are the real ones.** The skills are `grill-me`, `grill-with-docs`, `handoff`, `improve-codebase-architecture`, `tdd`, `setup-matt-pocock-skills` and `diagnosing-bugs`. The owner deletes the personal duplicates on the Mac.
7. **One-off notes stay off master.** They live on their issue or on a throwaway `research/<slug>` branch. Anything lasting moves into an ADR, a runbook or a reference note.
8. **Comments may cite an ADR or an issue/PR number.** They never cite a plan, a PRD file, an audit, a research note or a rule file. Two exceptions survive:
   - a comment naming the file the code serves;
   - a math `Reference:` line, or a test's pinned numbers, naming the outside source. It may name a reference note only when that note alone holds the fact.
9. **The kill-list readings bind new tests.** `testing.md` carries them as one-line examples of the test bar.
10. **Rule files keep the rules; ADRs keep the reasons.** Once the numerical-rigor ADR and the ADR 0022 amendment land, `numerical-rigor.md` and `temporal-rigor.md` keep only their do/don't lists and link their ADR.
11. **The sign-off table goes.** That is the "never edit without owner sign-off" table at `docs/doc-authority.md:53-64`.

Settled by earlier rulings, so not asked:

- The provenance block (`Formula` / `Reference` / `Canonical implementation` / `Validated against`) stays. It is now the only record of where a concept lives (◆).
- The math-registry half of "update both registries" goes, because `math-sources-of-truth.md` is cut. The engine-map half stays, since `engine-authority-map.md` is kept and slimmed.
- `add-fastapi-endpoint` gains the regeneration step for the OpenAPI snapshot and the Frontend types.

Two consequences follow from decision 6:

- The repo's `grill-me`, `grill-with-docs` and `wayfinder` call a `grilling` skill that exists only in `~/.codex/skills`, so the handoff copies it into the repo.
- The thermo skill is personal-only, and it still flags files over 1,000 lines. That contradicts "file size is never a finding" (owner, 2026-09-15). The handoff vendors it into the repo without the file-size rules, so the gate travels with the repo.

## Target layout

| File | Holds | Loads |
|---|---|---|
| `AGENTS.md` | Every shared rule: provider boundary, philosophy, gates, code/comment/doc rules, keeping and cutting, hard rules, Codex routing | Codex reads it; Claude reads it through `CLAUDE.md` |
| `CLAUDE.md` | `@AGENTS.md` and Claude-only lines | Claude, every session |
| `.claude/CLAUDE.md` | Dev commands only (services, containers, lint, targeted tests) | Claude, every session |
| `.claude/rules/angular.md`, `dotnet.md`, `python.md` | Stack conventions | Claude, only for their folder |
| `.claude/rules/testing.md` | The test bar, per-stack test conventions, how to run targeted tests | Claude, every session |
| `.claude/rules/numerical-rigor.md`, `temporal-rigor.md` | Math and time rules | Claude, every session |

## `AGENTS.md` — new text

Keep these sections word for word:

- the title paragraph;
- "REQUIRED: IBKR market data → Alpaca orders" (`:5-19`). It is sacred.
- "GitHub publishing environment", retitled "GitHub publishing environment (Codex only)".

Cut these sections:

- "STOP: legacy IBKR bot control" (A1);
- "STOP: legacy IBKR broker navigation" (A2);
- "Repo map" (C3 / A6);
- "Stack rules" (replaced by the routing below);
- "Session kickoff checklist" (C12; its step 3 moves to "References" below);
- "Disclaimers" (C14).

New and changed sections:

```markdown
This file holds every rule Claude and Codex share. `CLAUDE.md` imports it, so edit rules here, never in a copy.

## Guiding philosophy

1. **Math rigor before stack hygiene.** <unchanged>
2. **Numerical claims require receipts.** Every ported indicator, strategy, or calculation ships with a golden fixture derived from the reference (with its attribution file) and a tolerance-pinned test.
3. **Sovereignty over the math.** <unchanged>
4. **Strict equivalence is the default.** <unchanged>
5. **Python owns the math.** Every number a user compares against another number has one canonical implementation, in `PythonDataService/`. A .NET or Angular copy is allowed only as a named exception — a stated reason (latency, layer-locality) and a parity test naming the Python file — listed in the canonical-math ADR. The provenance block in each file records where its concept lives; there is no separate registry.
6. **Time is `int64 ms UTC`, and the calendar is the source of truth.** <CLAUDE.md:24 wording; ends "`.claude/rules/temporal-rigor.md` holds the rules (ADR 0022)">

## Codex and Claude compatibility

- This file is the single source of shared rules. `CLAUDE.md` imports it with `@AGENTS.md` and adds only Claude-only lines.
- Claude loads `.claude/rules/*.md` itself: `angular.md`, `dotnet.md` and `python.md` only when it works in that folder; `testing.md`, `numerical-rigor.md` and `temporal-rigor.md` always. Codex reads the matching rule file before stack work, and a skill's `.claude/skills/<name>/SKILL.md` when the list below says it applies.
- The repo's skills are the real copies. A personal skill with the same name shadows the repo copy in Claude; delete the personal one.

## Authority by claim
<the table, with these changes>
- Merge the "Codex behavior" and "Claude behavior" rows into one: `Agent behavior | this file, then the routed rule file or skill | Surface the conflict`.
- "Mathematical port target" authority becomes "Pinned vendored reference, plus the golden fixture and parity test".
- "Engine ownership" authority becomes `docs/architecture/engine-authority-map.md` (drop "and its migration plan" — #2741 cuts it).
- The closing "`docs/doc-authority.md` is the router…" line goes in the PR that cuts `doc-authority.md`.

## Engine and math authority

- Which engine owns a job: `docs/architecture/engine-authority-map.md`. A PR that moves ownership edits it in the same PR.
- Where a math concept's canonical implementation lives: the provenance block in its file (the `learn-ai-validation` skill).

## Gates

**Three tiers.** Locally, run lint for the stack you touched plus the test that proves your change. Every PR runs every quick test — not marked `slow`, no Postgres, no browser — wherever it sits. The daily run adds the rest. CI green is the gate.

- Each CI job keeps its 2-minute cap. When CI no longer fits, the slowest tests get `slow` and move to the daily run; no new jobs are added. The cap binds CI jobs, not local runs.
- A red daily run opens an issue (or comments on the open one). Fixing it is ordinary bug work.
- Master requires one `CI passed` roll-up plus CodeQL.

**Regression tests.** A fix for a P0 or P1 bug ships with a regression test that fails before the fix and passes after. P0/P1 is the issue's label, or the author's call when there is no issue. Other fixes may skip it.

**Thermo review.** Before the first push that opens a PR, an independent reviewer — a fresh subagent or Codex, never the author — runs the `thermo-nuclear-code-quality-review` skill on the diff.

- Size sets the most reviewers. Count lines added or changed in Python code and Frontend logic (TypeScript, not templates or styles); deletions, docs, generated files (contract snapshots, `broker.types.ts`) and lockfiles don't count.

  | Added Python + Frontend logic | At most |
  |---|---|
  | under 1,000 lines | none |
  | 1,000+ | 1 |
  | 3,000+ | 2 |
  | 8,000+ | 3 |

  Use judgment under the cap: a mechanical diff (a move, a rename) may need fewer. Parallel reviewers split the deep focus so they don't overlap, and their findings merge into one fix batch.
- A PR that only deletes code gets at most one reviewer, however big.
- A money-path or math PR gets at least one reviewer at any size. The author decides whether a PR is one and says so in the PR description. Money path: orders, custody, flatten, arming budgets, kill switches, leases, fencing. Math: anything with a provenance block or a golden fixture.
- Each reviewer runs once. After the fixes, one scoped re-review of them, then push, whatever it finds; name any deferred gap in the PR description. Re-pushes for review comments or CI never re-trigger thermo.
- Major findings block: fix them, or record the owner's sign-off in the PR description. Minor findings are optional. File size is never a finding.
- Every other PR ships on CodeRabbit plus green CI.

**Math ports.** A port from a reference ships with a golden fixture (with attribution) and a tolerance-pinned test. That is all the paperwork: no `docs/references/` note, no reconciliation report. An accepted divergence is documented in the test and in the port's module docstring.

## Code, comments and docs

- **Code is the documentation.** A doc earns its place only by holding what code cannot: a decision and its why (an ADR in force), an operator procedure (a runbook), an outside fact (a reference note), or intent for work still being built (an open PRD, which lives in its issue). A doc that restates code goes, even when something links it.
- **One-off notes stay off master.** Research findings, plans, specs and adversarial reviews live on their issue or on a throwaway `research/<slug>` branch, never merged. Anything lasting moves into an ADR, a runbook or a reference note.
- **Comments cite ADRs and issues.** A code comment may name an ADR or an issue/PR number — never a plan, PRD file, audit, research note or rule file. A reason strong enough to cite needs an ADR; otherwise say it in the comment in one line. Two kinds may name a doc: a comment naming the file the code serves (a served doc, the runbook that runs a script), and a math `Reference:` line or a test's pinned numbers, which name the outside source — or a reference note when only that note holds the fact. Golden-fixture attribution files are fixture paperwork, not comments.

## Keeping and cutting

- Dead code — nothing reachable uses it: no route, caller, UI entry, scheduled job or running script, and registries that dispatch by name count as callers — goes with its tests, docs and config.
- Two things stay even when nothing reads or calls them: money trails (orders, fills, fees, cash, positions, P&L, receipts, transactions) and validated volatility and options math (a test proves its numbers). Their routes and plumbing still go when dead. Clerk stores are never deleted or moved.
- An opt-in feature that no checked-in config turns on is not dead.

## Hard rules

- Never commit secrets, API keys, or connection strings. `.env` files only.
- Never leave `console.log`, `print()`, or `Console.WriteLine` in committed code. Use the structured logger for each stack.
- Never write silent exception handlers (`catch {}`, `except: pass`). Handle explicitly or let it propagate with context.
- When editing an existing file, follow the patterns already in that file. Don't reformat or restyle on the way through.
- Don't introduce new dependencies without justification. State the alternative considered and why it was rejected.
- Don't duplicate utility functions — search first.

## Skills
<the current list; replace "Agent tooling auto-discovers these from `.claude/skills/`" (A7) with "Codex: read the skill's `SKILL.md` when its task applies.">

## References

If the task involves a reference repo, check `references/` for a vendored copy. If it is not there, ask the owner whether to vendor it.
```

The `receiptLabel` and `app-asset-identity` hard rules move to `angular.md` (Frontend-only). The symbol-selection rule is already there (A9).

## `CLAUDE.md` — whole new file

```markdown
@AGENTS.md

<!-- Shared rules live in AGENTS.md. Put only Claude-only lines below. -->
```

Every line of today's `CLAUDE.md` is either in the new `AGENTS.md` or on the kill list:

- the provider boundary is AGENTS `:5-19`;
- principles 1–6 are rewritten above;
- the authority hierarchy is superseded by "Authority by claim", which already covers "surface conflicts";
- the hard rules are rewritten above;
- C1–C14 are cut.

## `.claude/CLAUDE.md`

- Keep `:1-43` (Quick start, Services, Containers).
- Cut `:45-243` (D1, D2).
- Replace "Running Tests" and "Linting":

```markdown
## Running tests

Run the test that proves your change; CI runs the rest.

cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/<path>::<test>   # Python, host venv
cd Frontend && npx ng test --include='src/app/<path>/<name>.spec.ts'                                   # Frontend, host, one exact spec file
# .NET: no dotnet on the owner's Mac — CI runs Backend.Tests and `dotnet format`

## Linting

ruff check PythonDataService/app/ PythonDataService/tests/   # Python
cd Frontend && npx eslint src/                               # Frontend (what CI runs)
```

The "(120 s hard limit)" lines and the `run_fast_tests` gate command go.

## `.claude/rules/`

**`angular.md`**

- Add frontmatter `paths: ["Frontend/**"]`.
- Cut `:3` (R1), Testing `:80-86` (R13) and Common pitfalls `:111-115` (R14). Keep `:116`.
- Add the `receiptLabel` and `app-asset-identity` rules from `AGENTS.md`'s hard rules, word for word.

**`dotnet.md`**

- Add frontmatter `paths: ["Backend/**", "Backend.Tests/**"]`.
- Cut `:3` (R1), Testing `:50-57` (R16) and Common pitfalls `:59-67` (R17).
- R15: replace "Static resolver classes with `[QueryType]` / `[MutationType]`" with "Resolvers live in `[ExtendObjectType]` classes registered with `AddTypeExtension<T>()` on the `AddQueryType<Query>()` root".
- X3: "Moq" only.
- The four prescriptions no code uses yet (union results, DataLoader, `IDbContextFactory`, `IRequestExecutor`) stay as guidance for new code.

**`python.md`**

- Add frontmatter `paths: ["PythonDataService/**"]`.
- Cut `:3` (R1), Testing `:90-97` (R19) and Common pitfalls `:99-109` (R20). Keep "Naked `dict` responses".
- R18: "Copy-on-write is the default in pandas 3".
- X2: "`respx`" only.
- The project-scope ruff paragraph stays: lint is still local.

**`testing.md`** (always-on)

- Cut `:3` (R1), `:9` (the regression rule moves to `AGENTS.md` Gates) and `:68` (R4).
- R3: `pytest.ini`, not `pyproject.toml`.
- X2: `respx` only. X3: Moq only.
- Add, first after Philosophy:

```markdown
## The test bar

A test earns its place when it proves an outcome a user or caller sees that no other test already proves. Five kinds fail the bar: (1) trivial, (2) copy and doc pins, (3) duplicates — keep the strongest, (4) mock theater, (5) retired features. A test that fails the bar is deleted outright — git history is the archive; no skip markers, quarantine folders or moves to the daily run. The same bar governs every new test.

How the bar reads in practice:
- A test whose subject is a state branch stays even when its only visible sign is a short label. Only a test whose subject is the copy itself is a copy pin.
- A shared formatter's own spec owns its wording. A component test that only repeats it is a duplicate.
- A test that asserts only the order of a fencing step stays when the order is the safety property and the end state looks the same either way.
- An absence check ("never says X") stays when it catches a specific false claim.
- A lint written as a test stays while the rule it enforces is in force.
- A tombstone test — one that only checks a retired feature stays gone — fails as kind 5.
- A parity test that has never run (collects nothing, or reads a missing path) proves nothing and goes.
- A golden fixture that records an outside fact (frozen vendor data) stays with its test, even when the test runs no app code. A fixture's regeneration script is part of the fixture.
- Text and AST pins on vendored reference algorithms are math-parity plumbing, not copy pins.
- On the money path, when unsure, keep.
```

- Numerical tests, item 5: replace it with "Untested existing math gets its test when it is touched. Until then its provenance block says `Validated against: NONE — pending fixture`." This drops the `math-sources-of-truth.md` tracking.
- Replace "Two-minute change-gate budget" and "Pre-push test-suite hygiene" (`:78-112`) with:

```markdown
## Running tests

- Locally, run the test that proves your change — and, when you edit a shared helper, the tests of what imports it. CI runs every quick test; the daily run runs everything (AGENTS.md → Gates).
  - Python: `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/<targeted paths>` (host venv)
  - Frontend: `npx ng test --include='src/app/<exact>.spec.ts'` (exact spec file, never a directory glob)
- Mark a test `@pytest.mark.slow` when it needs heavy infrastructure or pushes a CI job past its 2-minute cap. It runs daily. Never delete numerical-parity or regression coverage to fit the cap.
- Before calling a failure "not mine", run the same command on `origin/<base-branch>`. A pre-existing failure goes in the PR description.
- <keep the container-state hygiene and flag-ledger bullets as they are>
```

- Reconciliation tests `:121` (R5): "Documents any accepted divergence inline in the test and in the port's module docstring".

**`numerical-rigor.md`** (always-on)

Now:

- Cut `:3` (R1).
- R6: "Attribution file (`attribution.md` or `README.md`), registered in `tests/fixtures/golden/manifest.json`".
- Cut the `docs/references/` clause at `:74` (R7).
- Cut "Reconciliation reports" `:95-104` (R8) and the `:139-140` clause (R9).
- R10: point the two anti-patterns at `temporal-rigor.md`.
- Keep the "Timestamp rigor → Moved" stub (14+ files cite it).

After the numerical-rigor ADR lands:

- Keep only the do/don't content: equivalence levels, default tolerances, the loosening rule, fixture contents and lifecycle, warmup, floating-point hygiene, the taxonomy table, the acceptance gate, sovereignty and anti-patterns.
- Add one line linking the ADR. The "why" prose moves to the ADR.

**`temporal-rigor.md`** (always-on)

Now:

- R11: `app/broker/ibkr/minute_assembler.py`.
- R12: drop "(CI-enforceable with grep)" from the heading.

After the ADR 0022 amendment lands, cut the rationale paragraphs:

- the canonical-format rationale;
- "closest constructible instant";
- the "Readable time in exported CSVs" background;
- the live-subscription reasoning.

Keep each rule and link ADR 0022.

## Skills (`.claude/skills/`, write-protected in the sandbox — edit unsandboxed)

**Add:**

- `grilling/` — copy `~/.codex/skills/grilling/SKILL.md` (10 lines). It fixes the broken pointer in `grill-me`, `grill-with-docs`, `wayfinder` and `improve-codebase-architecture` (S5).
- `thermo-nuclear-code-quality-review/` — copy `~/.claude/skills/thermo-nuclear-code-quality-review/SKILL.md`, minus every file-size rule:
  - "giant files" in the description;
  - `:33-37` (the 1k-line rule);
  - `:80`, `:94`, `:119`, `:143`;
  - `:161` (heading 5);
  - `:175`, `:185`.

  The size-scaled reviewer count lives in `AGENTS.md`, not in the skill.

**Keep:** `grill-me`, `grill-with-docs`, `handoff`, `improve-codebase-architecture`, `tdd`, `setup-matt-pocock-skills`, `diagnosing-bugs` (S2–S4 overturned: the repo wins). Fix these within them:

- S6: `domain-modeling` and `improve-codebase-architecture` use `docs/architecture/adrs/`, not `docs/adr/`.
- S7: `code-review` and `wayfinder` point at `.claude/skills/setup-matt-pocock-skills/issue-tracker-github.md` for tracker operations. The tracker is GitHub via `gh`, and there is no `docs/agents/`.

**`learn-ai-validation`:**

- Keep the provenance block and its four fields. Re-scope it as "the only record of a concept's canonical implementation".
- `Reference:` names the primary source (paper, vendored file, URL). It names a reference note only when that note alone holds the fact.
- Workflow step 2: replace the registry lookup with "search for an existing canonical (`grep -rn "Canonical implementation"`). If the concept lives elsewhere, call it. A copy outside Python needs an exception in the canonical-math ADR and a parity test."
- "Untouched legacy": drop the registry sentence.
- Rewrite "Single source of truth" and "Layer detection" to principle 5 (Python owns the math, named exceptions only). This removes the S16 contradiction.
- Cut "Inline critical rules" `:90-127` (S15), the S17 line and the S18 per-port-notes line.
- Pointers: drop the registry line.
- Anti-patterns:
  - "A duplicate … without updating `docs/math-sources-of-truth.md`" → "A copy outside Python with no entry in the canonical-math ADR".
  - "Canonical implementation: this file in a .NET or Frontend math function without a parity test naming the Python canonical" stays.

**Other skills:**

- `port-indicator`: S9, S10, S11.
- `extract-math-from-paper`: S12, S13.
- `reconcile-backtest`: S14 ("encode the accepted tolerance in the test" stays).
- `add-fastapi-endpoint`:
  - S22 and S23.
  - Add a final step: "Regenerate the contract: `python PythonDataService/scripts/export_openapi_contract.py`, then `cd Frontend && npm run codegen:openapi`. Commit `contracts/openapi/python-data-service.openapi.json` and `Frontend/src/app/api/broker.types.ts`. Any change to a serialized model needs this, not just a new route."
- `research`: replace step 3 with "Commit the file on a throwaway `research/<slug>` branch (never merged) and link it from the issue. With no issue, leave it in the scratchpad and say where."
- `implement`: S8 ("lint plus the test that proves the change; CI runs the rest").
- `build-angular-component`: S20, S21.
- `write-graphql-resolver`: S19 (use the R15 pattern).
- `meta-propose-skill`: S24.
- `auto-research-tick`: cut (S1).

## Commands

- `test-all.md` goes (K1).
- `lint-all.md` loses its .NET step (X1; CI runs it).
- `health-check.md` stays.

## Docs

- `docs/doc-authority.md:53-64`: the sign-off table goes in the rule-rewrite PR. The rest of the file follows #2741: slim now, cut with or after the checker cut.
- Check `scripts/check_documentation_contract.py`'s class map for a matching `protected-canonical` list (`:37` names `math-sources-of-truth.md`), and change it in the same commit.

## Owner steps (outside the sandbox, after the vendored skills merge)

Delete from `~/.claude/skills`:

- `grill-me`, `grill-with-docs`, `handoff`, `improve-codebase-architecture`, `tdd`, `setup-matt-pocock-skills`;
- `diagnose`;
- `thermo-nuclear-code-quality-review`.

`~/.codex/skills` is Codex's own set and is out of scope.

## Sequencing and hazards

1. **No gate vanishes early.** The rule rewrite lands with, or before, every retired-row cut (C7, C9, R2, A8, the paperwork rows).
2. **The canonical-math ADR lands with, or before, the rule rewrite.** Principle 5 and `learn-ai-validation` name it.
   - Principle 5 keeps its number, so the ~20 comments citing "guiding philosophy #5" still resolve until they are re-pointed.
   - The `math-sources-of-truth.md` cut lands in the same PR as principle 5, the `testing.md` item 5 change and the `learn-ai-validation` change.
3. **Rule-file sections stay until their ADRs land.** The sections of `numerical-rigor.md` and `temporal-rigor.md` that comments cite stay until the numerical-rigor ADR and the ADR 0022 amendment land (#2746). Only then do the files shrink to rules plus a link.
4. **The docs checker pins this area.** It requires `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/commands/` (non-empty) and `.claude/hooks/`. It also requires the `## Codex and Claude compatibility` heading in `AGENTS.md`, and checks every Angular/.NET version `AGENTS.md` mentions. Run `pytest PythonDataService/tests/contracts/test_documentation_contract.py`.
5. **Served copy.** `docs/indicator-reliability-methodology.md:1367` links `.claude/CLAUDE.md`. If that link changes, edit its served copy in `Frontend/src/assets/docs/` too.
6. **Sandbox.** `.claude/skills` and `.claude/hooks` are write-protected, so edit them unsandboxed.
7. **Main checkout.** Sessions read rules from the main checkout, so the new rules take effect only after merge and a pull there. Never switch branches in the main checkout: live clerks bind-mount it.
8. **Check the `@AGENTS.md` import.** Open a fresh session after the merge and confirm that a provider-boundary line appears in its loaded context.

## Agent memories to update when the rewrite lands

- `feedback_pr_workflow.md` — thermo on every PR → the size tiers and the money/math floor.
- `feedback_big_slices_get_two_opus_reviews.md` — superseded by the size tiers; keep its "split the deep focus, one merged fix batch".
- `feedback_no_repeat_review_rounds.md` — the cap stays, now per reviewer.
- `feedback_independent_review_never_self.md` — now in `AGENTS.md`; keep it as history.
- `feedback_scoped_frontend_runs_miss_ci.md` — "full `ng test` before push" is retired; CI is the gate.
- `feedback_file_size_limits_are_not_blocking.md` — now in the vendored skill.
- `project_lean_and_mean_wayfinder_2026_09_30.md` — the rules are in effect.
