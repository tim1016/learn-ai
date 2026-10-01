# learn-ai

A scientific platform for porting and validating trading logic. Reference implementations (LEAN, open-source backtesters, academic papers) are mined for math, then ported into this repo with strict numerical equivalence and vanishing external dependency.

This file holds every rule Claude and Codex share. `CLAUDE.md` imports it, so edit rules here, never in a copy.

## REQUIRED: IBKR market data → Alpaca orders

**Owner decision, 2026-09-16: retain Interactive Brokers as the live market-data
provider for every Alpaca Paper, Live Shadow, and Live bot. Alpaca handles
accounts, orders, and execution reports. Direct Alpaca market-data subscriptions
are prohibited; the owner rejects their additional subscription cost.**

The IBKR deprecations below apply to bot control and navigation, **never to the
read-only market-data connection, bars, or trading-status evidence**. Preserve
`IBKR_BROKER_ENABLED=true`, `IBKR_READONLY=true`, and distinct Gateway client IDs
on clerk agents. Repair Gateway connectivity instead of disabling the feed.
For provider wiring, account setup/removal, or launch failures, read
[the account runbook](docs/runbooks/add-an-alpaca-account.md) and
[ADR 0062's provider decision](docs/architecture/adrs/0062-broker-clerk-fleet-control-plane.md#retained-market-data-provider--owner-decision-2026-09-16).
Changing this provider boundary requires an explicit owner decision.

## Guiding philosophy

1. **Math rigor before stack hygiene.** This repo's primary job is porting mathematical logic from reference sources and proving numerical equivalence. Stack conventions matter but never override math correctness.
2. **Numerical claims require receipts.** Every ported indicator, strategy, or calculation ships with a golden fixture derived from the reference (with its attribution file) and a tolerance-pinned test.
3. **Sovereignty over the math.** Reference code is studied, ported, and then the dependency is eliminated. Vendored references in `references/` exist for audit, not for runtime use.
4. **Strict equivalence is the default.** Warmup bars, timestamp alignment, commission, and fill models must match the reference exactly. If they can't, that fact is documented in the port's module docstring.
5. **Python owns the math.** Every number a user compares against another number has one canonical implementation, in `PythonDataService/`. A .NET or Angular copy is allowed only as a named exception — a stated reason (latency, layer-locality) and a parity test naming the Python file — listed in the canonical-math ADR ([ADR 0068](docs/architecture/adrs/0068-python-owns-the-canonical-math.md)). The provenance block in each file records where its concept lives; there is no separate registry.
6. **Time is `int64 ms UTC`, and the calendar is the source of truth.** Every temporal value in flight, at rest, or on the wire is `int64 ms UTC`; ISO strings and `DateTime` are disallowed as wire/storage formats. Language-native datetime types (`pd.Timestamp`, `DateTime`, `Date`) are permitted only for local arithmetic inside a single function and must be converted back to `int64 ms UTC` before returning, persisting, or serializing. All scheduled session structure (trading days, session open/close, early closes, alignment) derives from a single canonical calendar module — no hardcoded session times. Real-time market liveness (halts) is a separate concern owned by the live feed. `.claude/rules/temporal-rigor.md` holds the rules (ADR 0022).

## Codex and Claude compatibility

- This file is the single source of shared rules. `CLAUDE.md` imports it with `@AGENTS.md` and adds only Claude-only lines.
- Claude loads `.claude/rules/*.md` itself: `angular.md`, `dotnet.md` and `python.md` only when it works in that folder; `testing.md`, `numerical-rigor.md` and `temporal-rigor.md` always. Codex reads the matching rule file before stack work, and a skill's `.claude/skills/<name>/SKILL.md` when the list below says it applies.
- The repo's skills are the real copies. A personal skill with the same name shadows the repo copy in Claude; delete the personal one.

## Authority by claim

Use the authority for the claim being changed; do not apply a universal source
ordering across unrelated domains.

| Claim | Authority | If sources disagree |
|---|---|---|
| Agent behavior | This file, then the routed rule file or skill | Surface the conflict |
| Product or system decision | Accepted ADR, with later explicit supersession winning | Record or ask for an explicit decision |
| Mathematical port target | Pinned vendored reference, plus the golden fixture and parity test | Surface the conflict; do not silently choose |
| Engine ownership | `docs/architecture/engine-authority-map.md` | Surface the conflict |
| Current runtime or wire shape | Manifest/config, generated contract, implementation, and executable tests | Docs describe this evidence; they do not override it |
| Framework behavior | Installed manifest version and official documentation for that version | Derive the version from the manifest, not a prose cache |
| Open defect | `docs/known-gaps.md` | Closed findings belong in durable decision history or Git history |

## Engine and math authority

- Which engine owns a job: `docs/architecture/engine-authority-map.md`. A PR that moves ownership edits it in the same PR.
- Where a math concept's canonical implementation lives: the provenance block in its file (the `learn-ai-validation` skill).

## Gates

**Three tiers.** Locally, run lint for the stack you touched plus the test that proves your change. Every PR runs every quick test — not marked `slow`, no Postgres, no browser — wherever it sits. The daily run adds the rest. CI green is the gate.

- Each CI job keeps its 2-minute cap. When CI no longer fits, the slowest tests get `slow` and move to the daily run; no new jobs are added. The cap binds CI jobs, not local runs.
- A red daily run opens an issue (or comments on the open one). Fixing it is ordinary bug work.
- Master requires one `CI passed` roll-up plus CodeQL.

**Regression tests.** A fix for a P0 or P1 bug ships with a regression test that fails before the fix and passes after. P0/P1 is the issue's label, or the author's call when there is no issue. Other fixes may skip it.

**Thermo review.** Before the first push that opens a PR, an independent reviewer — a fresh subagent or Codex, never the author — runs the repo's `thermo-nuclear-code-quality-review` skill (`.claude/skills/thermo-nuclear-code-quality-review/SKILL.md`, not a personal copy) on the diff.

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

Codex: read the skill's `SKILL.md` when its task applies.

- **port-indicator** — Port an indicator or strategy from a reference source into `PythonDataService/` with strict numerical equivalence
- **reconcile-backtest** — Diff two backtest runs trade-by-trade and classify divergence sources
- **extract-math-from-paper** — Transcribe equations from a PDF paper into testable Python with paper-section citations
- **add-fastapi-endpoint** — Add a new FastAPI endpoint exposing engine output to the frontend
- **write-graphql-resolver** — Write or debug a Hot Chocolate v15 resolver
- **build-angular-component** — Build or modify an Angular 22 component
- **meta-propose-skill** — When the same task shape repeats, propose a new skill instead of just doing the task
- **learn-ai-validation** — Add or touch math: the provenance block, the canonical-implementation search, parity tests
- **thermo-nuclear-code-quality-review** — The independent pre-PR review the Gates call for

## References

If the task involves a reference repo, check `references/` for a vendored copy. If it is not there, ask the owner whether to vendor it.

## GitHub publishing environment (Codex only)

Containerized agent sessions can fail to reach Podman or report unusable GitHub CLI credentials even when the host Git credential is valid. For a requested push, retry the host-side `git` operation with the permitted sandbox bypass; use the GitHub connector to create or update the pull request. Treat the container failure as an environment boundary, not as evidence that the remote is unavailable.
