# learn-ai

A scientific platform for porting and validating trading logic. Reference implementations (LEAN, open-source backtesters, academic papers) are mined for math, then ported into this repo with strict numerical equivalence and vanishing external dependency.

## Guiding philosophy

1. **Math rigor before stack hygiene.** This repo's primary job is porting mathematical logic from reference sources and proving numerical equivalence. Stack conventions matter but never override math correctness.
2. **Numerical claims require receipts.** Every ported indicator, strategy, or calculation ships with (a) a golden fixture derived from the reference, (b) a tolerance-pinned test, and (c) a citation in `docs/references/`.
3. **Sovereignty over the math.** Reference code is studied, ported, and then the dependency is eliminated. Vendored references in `references/` exist for audit, not for runtime use.
4. **Strict equivalence is the default.** Warmup bars, timestamp alignment, commission, and fill models must match the reference exactly. If they can't, that fact is documented in the port's module docstring.

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
