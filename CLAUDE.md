# learn-ai

A scientific platform for porting and validating trading logic. Reference implementations (LEAN, open-source backtesters, academic papers) are mined for math, then ported into this repo with strict numerical equivalence and vanishing external dependency.

## Guiding philosophy

1. **Math rigor before stack hygiene.** This repo's primary job is porting mathematical logic from reference sources and proving numerical equivalence. Stack conventions matter but never override math correctness.
2. **Numerical claims require receipts.** Every ported indicator, strategy, or calculation ships with (a) a golden fixture derived from the reference, (b) a tolerance-pinned test, and (c) a citation in `docs/references/`.
3. **Sovereignty over the math.** Reference code is studied, ported, and then the dependency is eliminated. Vendored references in `references/` exist for audit, not for runtime use.
4. **Strict equivalence is the default.** Warmup bars, timestamp alignment, commission, and fill models must match the reference exactly. If they can't, that fact is documented in the port's module docstring.
5. **Single source of truth for any numerical answer.** Math may live in any layer that fits the use case; what's not negotiable is that there is **one** canonical implementation per concept. Duplicates are acceptable only when they exist for a real reason (latency, layer-locality, vendor parity) and carry a parity test naming the canonical file in their provenance block. The provenance block (`Formula`/`Reference`/`Canonical implementation`/`Validated against`) is enforced by the `learn-ai-validation` skill.
6. **Timestamps are `int64 ms UTC` at all boundaries.** Wire and storage must always use `int64 ms UTC`; ISO strings and `DateTime` are disallowed as wire/storage formats. Language-native datetime types (`pd.Timestamp`, `DateTime`, `Date`) are permitted only for local arithmetic inside a single function and must be converted back to `int64 ms UTC` before returning, persisting, or serializing. See `.claude/rules/numerical-rigor.md` → "Timestamp rigor" for the full policy, the two conversion boundaries, and the ban list.

## Repo map

- `Frontend/` — Angular 21 SPA (standalone components, signals, zoneless, Vitest)
- `Backend/` — .NET 10 GraphQL API (Hot Chocolate v15, EF Core, Postgres)
- `Backend.Tests/` — xUnit test suite for Backend
- `PythonDataService/` — FastAPI data proxy + backtesting engine (pandas, Polygon.io)
- `docs/architecture/` — ADRs and system diagrams
- `docs/domain/` — Trading concepts, glossary, invariants
- `docs/references/` — Per-port notes: what was ported, from where (repo + commit), with what tolerance
- `references/` — Vendored reference code (LEAN snippets, backtesters) under version control
- `.claude/skills/` — Lazy-loaded skills for recurring tasks
- `.claude/rules/` — Stack-specific rules, referenced from here but read only when relevant

## Authority hierarchy

When sources conflict, resolve in this order:

1. **Vendored references** in `references/` (ground truth for what we're porting)
2. **Official docs** (angular.dev, learn.microsoft.com, chillicream.com, fastapi.tiangolo.com, pandas.pydata.org)
3. **`.claude/rules/*.md`** in this repo
4. **Model training knowledge**

**When conflicts arise, surface them.** Do not silently pick. State the conflict, cite the sources, ask the user which to follow.

## Skills available in this repo

Claude Code auto-discovers these from `.claude/skills/`. Invoke directly or let them auto-trigger:

- **port-indicator** — Port an indicator or strategy from a reference source into `PythonDataService/` with strict numerical equivalence
- **reconcile-backtest** — Diff two backtest runs trade-by-trade and classify divergence sources
- **extract-math-from-paper** — Transcribe equations from a PDF paper into testable Python with paper-section citations
- **trading-domain** — Domain knowledge (bar semantics, timestamp conventions, strategy invariants). Auto-loads when trading vocabulary appears
- **add-fastapi-endpoint** — Add a new FastAPI endpoint exposing engine output to the frontend
- **write-graphql-resolver** — Write or debug a Hot Chocolate v15 resolver
- **build-angular-component** — Build or modify an Angular 21 component
- **meta-propose-skill** — When the same task shape repeats, propose a new skill instead of just doing the task

## Stack rules

Full conventions live in `.claude/rules/`. Read the relevant file before significant changes:

- `.claude/rules/angular.md` — Angular 21 conventions (signals, zoneless, Signal Forms, Vitest)
- `.claude/rules/dotnet.md` — .NET 10 + Hot Chocolate v15 conventions
- `.claude/rules/python.md` — FastAPI, pandas, async conventions
- `.claude/rules/testing.md` — Per-stack testing standards
- `.claude/rules/numerical-rigor.md` — The core scientific rules (tolerances, golden fixtures, reconciliation taxonomy)

## Hard rules (apply to every task)

- Never commit secrets, API keys, or connection strings. `.env` files only.
- Never leave `console.log`, `print()`, or `Console.WriteLine` in committed code. Use the structured logger for each stack.
- Never write silent exception handlers (`catch {}`, `except: pass`). Handle explicitly or let it propagate with context.
- Every bug fix ships with a regression test that fails before the fix and passes after.
- **Run project-scope lint and tests before pushing.** The pre-commit hook (`lint-staged`) only checks the staged paths in *one* commit; cross-file drift (unused imports left after a refactor, sort order, ESLint warnings introduced indirectly) slips through. Run the same command CI uses for whatever stack you touched: `ruff check PythonDataService/app/ PythonDataService/tests/`, `npx eslint Frontend/src/ --max-warnings 0`, or `dotnet format podman.sln --verify-no-changes`. Per-file checks are for iteration; project-scope is for the push. See `.claude/rules/python.md` (lint scope) and `.claude/rules/testing.md` (pre-push test hygiene + how to baseline against inherited failures).
- **Run the `thermo-nuclear-code-quality-review` skill before pushing any PR, and address every major finding before push.** Lint and tests catch syntax and regressions; the thermonuclear review catches structural code-quality regressions — sprawling files, spaghetti branching, missed "code judo" simplifications, abstraction leakage, file-size explosions, boundary leaks, canonical-helper duplication — that lint and tests cannot see. It is the gate between "the diff compiles" and "the diff is safe to ship". The skill (`~/.claude/skills/thermo-nuclear-code-quality-review/SKILL.md`) is marked `disable-model-invocation: true`, so **I cannot launch it myself** — I must pause before `git push` and ask the user to invoke it on the current branch's diff. **Triage by severity:** every **major** finding (structural regression, file-size explosion past ~1k lines, spaghetti branching, missed code-judo, abstraction-boundary leak, canonical-helper duplication, hacky/magical abstraction) is a blocker — fix it in-branch before push, or, if explicitly accepted, document the reason in the PR description with the user's sign-off. **Minor** findings (cosmetic nits, low-conviction style notes, micro-suggestions) are optional — fix if cheap, defer if not; do not let them block the push or churn the diff. The rule applies **only on the first push that opens the PR**. Re-pushes that address PR review comments (CodeRabbit, human reviewer, CI failures) do **NOT** re-trigger thermo — the gate is one-shot per PR. Re-pushes still require project-scope lint + the relevant test surface to be green.
- Every port from a reference source ships with (a) a golden fixture test, (b) a `docs/references/` note, (c) the tolerance used and why.
- When editing an existing file, follow the patterns already in that file. Don't reformat or restyle on the way through.
- Don't introduce new dependencies without justification. State the alternative considered and why it was rejected.
- Don't create new files when editing an existing one works. Don't duplicate utility functions — search first.
- Validate inputs at system boundaries (API endpoints, external data ingestion). Internal trusted code doesn't need paranoid guards.

## Session kickoff checklist

When starting a session on this repo, before the first significant edit:

1. Read the user's task. Identify the skill that matches, if any.
2. If porting math or working with trading concepts, `.claude/skills/trading-domain/SKILL.md` should auto-load. If it didn't, load it manually.
3. Before touching stack code, read the relevant `.claude/rules/*.md`.
4. If the task involves a reference repo, check `references/` for a vendored copy. If not present, ask the user whether to vendor it or fetch via GitHub MCP.

## Disclaimers

This repo is for research and education. Nothing produced here is financial advice. The backtesting engine is a research tool; live trading requires separately validated infrastructure.
