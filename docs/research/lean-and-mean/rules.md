# Kill list — CLAUDE.md, rules, repo skills and commands (#2715)

Part of map #2700. Read at **`87b8e261`** (`origin/master`, the map's charting SHA).
Plan only — nothing here is cut.

**Area:** `CLAUDE.md`, `.claude/CLAUDE.md`, `AGENTS.md`, `Frontend/CLAUDE.md`,
`Backend/CLAUDE.md`, `PythonDataService/CLAUDE.md`, `.claude/rules/*.md`,
`.claude/skills/*`, `.claude/commands/*`.

**Kinds:** *untrue* (code or config disproves it), *inert* (restates a default or
duplicates a named canonical line), *retired* (encodes a rule the map's locked rules
replace), *whole dead skill/command*. Retired rows replace nothing here; the new text is
the map's "Rule rewrite" fog.

## Facts that drive many rows

1. **The rule files load into every Claude Code session.** None of
   `.claude/rules/*.md` has `paths:` frontmatter, and this session's context carried
   all six as project instructions, next to `CLAUDE.md` and `.claude/CLAUDE.md`.
   So every "read the rule file when…" line is inert, and any copy of rule text in
   another always-loaded file is loaded twice.
2. **Personal skills shadow same-named repo skills on the owner's Mac.** This
   session's skill listing served the `~/.claude/skills` descriptions for `grill-me`,
   `grill-with-docs`, `handoff`, `improve-codebase-architecture` and `tdd` (e.g. the
   listed `tdd` text equals `~/.claude/skills/tdd/SKILL.md:3`). The repo copies differ
   from the personal copies (`diff -q`), and never load.
3. **No `dotnet` on the owner's Mac.** `command -v dotnet` is empty and `~/.dotnet`
   holds only first-run sentinels. .NET commands run in CI (`ci.yml:207`, `:253`) or
   in the `my-backend` SDK container (`compose.yaml` backend `command: dotnet watch run`).
4. **No MCP server comes from the repo.** `.claude/settings.json:3-26` declares
   `mcpServers`, which Claude Code does not read from `settings.json`; there is no
   `.mcp.json`, `references/MCP-SETUP.md` (named at `settings.json:3`) is missing,
   and no Postgres/GitHub/Polygon MCP is present in sessions.

## Kill list

### `CLAUDE.md` (always loaded)

| # | Item | Kind | Evidence |
|---|---|---|---|
| C1 | `CLAUDE.md:19` clause (c) "a citation in `docs/references/`" | retired | Locked rule: math port paperwork = golden fixture (with attribution) + tolerance-pinned test. Clauses (a) and (b) stay. |
| C2 | `CLAUDE.md:33` `docs/domain/` repo-map entry | untrue | `docs/domain` does not exist at `87b8e261`. |
| C3 | `CLAUDE.md:25-37` repo map (other lines) | inert | A cache of `ls`; `:34` also restates the retired per-port-note rule (C1). |
| C4 | `CLAUDE.md:37` "`.claude/rules/` … read only when relevant" | untrue | Fact 1: the rule files load every session. |
| C5 | `CLAUDE.md:50-61` "Skills available" list | inert | The harness lists every skill's description each session; this list has 8 entries for 25 repo skills. `:57` `trading-domain` is untrue: no such skill in `.claude/skills/` or `~/.claude/skills/`. |
| C6 | `CLAUDE.md:63-72` "Stack rules" pointers + "Read the relevant file before significant changes" | inert | Fact 1: already in context. |
| C7 | `CLAUDE.md:79` "Every bug fix ships with a regression test…" | retired | Locked: regression tests mandatory only for money-path and math fixes. |
| C8 | `CLAUDE.md:80` "run the same lint command CI uses" — the `eslint … --max-warnings 0` and `dotnet format` parts | untrue | CI runs `npx eslint src/` with no `--max-warnings` (`ci.yml:143`); `dotnet format` cannot run on the host (fact 3). The targeted-test half belongs to the CI-shape fog; leave it. |
| C9 | `CLAUDE.md:81` thermo on every PR (whole bullet, incl. one-shot-per-PR and file-size notes) | retired | Locked: thermo only on money-path and math PRs; others ship on CodeRabbit + green CI. |
| C10 | `CLAUDE.md:82` port paperwork bullet | retired / inert | (b) `docs/references/` note is retired (C1); (a) and (c) repeat `:19`. |
| C11 | `CLAUDE.md:86-87` "Don't create new files…" / "Validate inputs at system boundaries…" | inert | Restate the Claude Code harness defaults (prefer editing existing files; validate only at system boundaries). Model-relative — judged against this session's harness prompt, not by a run. |
| C12 | `CLAUDE.md:93-95` kickoff steps 1–3 | inert / untrue | Step 1 is default behavior; step 2 names the missing `trading-domain` skill (untrue); step 3 is fact 1. |
| C13 | `CLAUDE.md:96` "…or fetch via GitHub MCP" | untrue | Fact 4. The "check `references/` first, else ask" part stays. |
| C14 | `CLAUDE.md:98-100` Disclaimers | inert | Changes no agent behavior. |

### `AGENTS.md` (Codex entry point; pinned by `scripts/check_documentation_contract.py`)

| # | Item | Kind | Evidence |
|---|---|---|---|
| A1 | `AGENTS.md:21-31` "STOP: legacy IBKR bot control" | untrue | The guarded areas are gone: `Frontend/src/app/components/broker/` has no `bots/` or `bot-control/`; `:27` itself says the Python surface is deleted. Only redirect routes remain (A2). Not the IBKR feed — that boundary (`:5-19`) is sacred and stays. |
| A2 | `AGENTS.md:33-51` "STOP: legacy IBKR broker navigation" | inert | Duplicates the at-site guard `Frontend/src/app/app.routes.ts:42-47` ("must remain redirect-only; do not attach UI, providers, guards…") over the routes at `:49-62`. |
| A3 | `AGENTS.md:56` clause (c) and `:146` clause (b) `docs/references/` | retired | As C1. |
| A4 | `AGENTS.md:59` "Python owns all math" | untrue | Replaced in `CLAUDE.md` by `da648349` (2026-04-26): `CLAUDE.md:22` "Math may live in any layer"; `learn-ai-validation/SKILL.md:80` agrees. Codex still gets the old rule. |
| A5 | `AGENTS.md:60` "See `numerical-rigor.md` → Timestamp rigor for the full policy…" | untrue | That section is a "Moved" stub (`numerical-rigor.md:76-80`); the policy is `temporal-rigor.md`. |
| A6 | `AGENTS.md:70` `docs/domain/` | untrue | As C2. |
| A7 | `AGENTS.md:119` "Agent tooling auto-discovers these from `.claude/skills/`" | untrue | Contradicts `AGENTS.md:85-87` (for Codex these are committed sources, not auto-discovery). The list under it is Codex's routing and stays. |
| A8 | `AGENTS.md:145` regression test on every fix | retired | As C7. |
| A9 | `AGENTS.md:149` symbol-selection rule | inert | Duplicates `angular.md:34-59` (ADR 0066), which `AGENTS.md:131-138` routes Codex to. |

### `.claude/CLAUDE.md` (always loaded)

| # | Item | Kind | Evidence |
|---|---|---|---|
| D1 | `.claude/CLAUDE.md:45-51` Repo Structure | untrue / inert | "Angular 21" vs `Frontend/package.json:32` `^22.0.0`; the rest duplicates `CLAUDE.md:25-30`. |
| D2 | `.claude/CLAUDE.md:55-243` the whole "Coding Guidelines" half (Angular, .NET, Python, Testing, General, and the pasted Angular best-practices list `:212-243`) | inert | Every line restates an always-loaded rule file (`angular.md`, `dotnet.md`, `python.md`, `testing.md`) or a `CLAUDE.md` hard rule. Inside it, these lines are also untrue or retired: `:68` "or `mutate()`" (contradicts `angular.md:18` and this file's `:236`); `:71` "must be `standalone: true`" (contradicts `:216`, `angular.md:11`); `:157` "Jest" (absent from `Frontend/package.json`); `:154`, `:210` regression test on every fix (retired, C7); `:178` "not exact floating-point values" (contradicts strict float, `numerical-rigor.md:18`). `:164`, `:167`, `:176` see X2/X3. |

`.claude/CLAUDE.md:1-43` (Quick start, services, commands, containers) stays, except the
.NET commands (X1). The file itself must survive — see Hazard 1.

### `Frontend/CLAUDE.md`, `Backend/CLAUDE.md`, `PythonDataService/CLAUDE.md` (loaded when Claude reads files there)

| # | Item | Kind | Evidence |
|---|---|---|---|
| F1 | `Frontend/CLAUDE.md:16-45` File Structure tree | untrue | "23 feature directories" — 31 exist; `market-data/`, `options-strategy-lab/`, `technical-analysis/`, `snapshots/` don't. "13 injectable services" — `services/` has 29 `*.service.ts`; `polygon.service.ts` and `replay-engine.service.ts` don't exist. |
| F2 | `Frontend/CLAUDE.md:51` "(only `portfolio.service.ts` wraps that in a local `gql<T>()`)" | untrue | `services/data-lab-session.service.ts` also does. |
| F3 | `Frontend/CLAUDE.md:49-50`, `:54` standalone/OnPush/signals/control flow | inert | Duplicates `angular.md:9-18`. |
| F4 | `Frontend/CLAUDE.md:56` `receiptLabel` | inert | Duplicates `CLAUDE.md:83` (always loaded). |
| F5 | `Frontend/CLAUDE.md:69` "Some components are large (options-strategy-lab, strategy-builder) — consider extracting" | untrue / retired | `options-strategy-lab` doesn't exist; file size is never a finding (owner, 2026-09-15). |
| B1 | `Backend/CLAUDE.md:32-33` "14 service interfaces / 14 implementations" | untrue | `Backend/Services/Interfaces` and `Implementation` hold 12 each. |
| B2 | `Backend/CLAUDE.md:47`, `:51-53`, `:58`, `:60`, `:63` | inert | Duplicate `dotnet.md:30`, `:24`, `:39`, `:47`, `:52-53`, `:56`. (`:59` "Moq" is the accurate line — see X3.) |
| P1 | `PythonDataService/CLAUDE.md:34` `.venv/Scripts/python.exe` | untrue | Windows layout; the owner's checkout has `PythonDataService/.venv/bin/python` and no `Scripts/`. |
| P2 | `PythonDataService/CLAUDE.md:9` daily command `python -m pytest tests app/engine/tests -v` | untrue | `daily-tests.yml:104` also runs `app/engine/strategy/spec/tests`, and `:111` runs a separate data-lake step. |
| P3 | `PythonDataService/CLAUDE.md:11` Lint `ruff check PythonDataService/app/` | untrue | CI lints `app/ tests/` (`ci.yml:274`); `python.md:19` and `CLAUDE.md:80` say project scope. |
| P4 | `PythonDataService/CLAUDE.md:16` "no dependencies — runs standalone" | untrue | `compose.yaml` `python-service` has `depends_on: redis (service_healthy)` and reads `POSTGRES_URL`. |
| P5 | `PythonDataService/CLAUDE.md:77-124` File Structure tree | untrue | "19 API route modules" — 60 exist; `routers/backtest.py` and `services/strategies/` don't exist; engine "37 files" — 180; research "30 files" — 153; `models/` is called the Pydantic home, but schemas live in `app/schemas/` (61 files, `python.md:44`). |
| P6 | `PythonDataService/CLAUDE.md:128-130`, `:139`, `:141-142` | inert | Duplicate `python.md:40-41`, `:14` and `testing.md:61-63`. `:141` see X2. (`:138` is the accurate `pytest.ini` line that R3 contradicts; keep it.) |

### `.claude/rules/`

| # | Item | Kind | Evidence |
|---|---|---|---|
| R1 | "Read when / before…" header line in `angular.md:3`, `dotnet.md:3`, `python.md:3`, `testing.md:3`, `numerical-rigor.md:3` | inert | Fact 1: the file is already loaded when the line is read. (For Codex, routing lives in `AGENTS.md:131-138`.) |
| R2 | `testing.md:9` "Every bug fix includes a regression test…" | retired | As C7. |
| R3 | `testing.md:59` "`asyncio_mode = "auto"` in pyproject.toml" | untrue | It is `PythonDataService/pytest.ini:2`; there is no `pyproject.toml`. |
| R4 | `testing.md:68` golden fixture attribution "in the filename or a sibling `README.md`" | inert | Duplicates `numerical-rigor.md:23-54` (the canonical fixture rule). |
| R5 | `testing.md:121` accepted divergences referenced in `docs/references/reconciliations/<name>.md` | retired | Paperwork beyond fixture + test. |
| R6 | `numerical-rigor.md:31-36` "Attribution file (`README.md` or `attribution.json`)" | untrue | Fixtures use `attribution.md` (19 dirs) or `README.md` (5); none has `attribution.json`; the registry is `tests/fixtures/golden/manifest.json` + `manifest.schema.json`. The rule that a fixture carries attribution stays (sacred). |
| R7 | `numerical-rigor.md:74` "…and in `docs/references/<construct-name>.md`" | retired | Tolerance reason in the test file stays; the note goes. |
| R8 | `numerical-rigor.md:95-104` "Reconciliation reports" section | retired | A mandatory report per reconciled port is paperwork beyond fixture + test. Existing reports → #2714. |
| R9 | `numerical-rigor.md:139-140` "…in the reconciliation report at `docs/references/reconciliations/<n>.md`" | retired | As R8. |
| R10 | `numerical-rigor.md:159-160` anti-patterns citing "Canonical format above" and "Timestamp rigor → Ban list" | untrue | Neither section is in this file; both are in `temporal-rigor.md:20`, `:105`. |
| R11 | `temporal-rigor.md:79` "Reference implementation: `app/broker/ibkr/bars.py` (`policy="strict"` …)" | untrue | The policy switch is `app/broker/ibkr/minute_assembler.py:366-451` (`policy: DuplicatePolicy = "strict"`), with `LiveBarCounters` at `:117` and `SPARSE_MINUTE_EMIT_GRACE_MS` at `:60`; `bars.py:1119` only uses it. |
| R12 | `temporal-rigor.md:105` heading "(CI-enforceable with grep)" | untrue | No CI step or test greps the ban list, and `ruff.toml:8-16` selects no `DTZ` rules; `app/services/data_quality_service.py:448` calls `_dt.utcnow()` on master. The ban list itself stays. |
| R13 | `angular.md:80-86` Testing | inert | Duplicates `testing.md:40-46` (`CLAUDE.md:70` names `testing.md` the per-stack testing authority). |
| R14 | `angular.md:111-115` Common pitfalls | inert | Each line restates a Non-negotiable in the same file (`:9`, `:12`, `:11`, `:18`, `:16`). `:116` (safe-navigation migration) is unique and stays. |
| R15 | `dotnet.md:31` "Static resolver classes with `[QueryType]` / `[MutationType]`" and `:62` "(they're static…)" | untrue | `Backend/Program.cs:140-144` wires `AddQueryType<Query>()` + `AddTypeExtension<…>()` over non-static `[ExtendObjectType]` classes (4 files); there are 0 `[QueryType]`/`[MutationType]` attributes and 0 static classes in `Backend/GraphQL/`. |
| R16 | `dotnet.md:50-57` Testing | inert | Duplicates `testing.md:48-55`. |
| R17 | `dotnet.md:59-67` Common pitfalls | inert | Each line restates `:30`, `:32`, `:34`, `:33`, `:21`, `:23`, `:55`. |
| R18 | `python.md:62` "Copy-on-write behavior (pandas 2.x default)" | untrue | `requirements-heavy.txt:5` pins `pandas==3.0.1`; copy-on-write became the default in 3.0. |
| R19 | `python.md:90-97` Testing | inert / untrue | Duplicates `testing.md:57-64`. `:97` "Numerical tests: assert on shapes, dtypes, and value ranges" contradicts strict-float golden equivalence (`numerical-rigor.md:18`, `testing.md:35-36`). |
| R20 | `python.md:99-109` Common pitfalls (except `:104` "Naked `dict` responses") | inert | Each line restates `:41`, `:94`, `:53`, `:86`, `:76`, `:82`, `:67`, `:58-60`. |

Keep `numerical-rigor.md:76-80` (the "Moved" stub): it is a redirect target — see Orphans.

### Cross-file rows

| # | Item | Kind | Evidence |
|---|---|---|---|
| X1 | Host `.NET` commands: `CLAUDE.md:80` (`dotnet format`), `.claude/CLAUDE.md:24`, `:33` ("C# (local)"), `Backend/CLAUDE.md:8-11`, `:16` ("Tests run locally…"), `testing.md:104`, `.claude/commands/lint-all.md:13-16`, `.claude/commands/test-all.md:8-11` | untrue (owner's Mac) | Fact 3. The command lines are right for CI and the `my-backend` container, not for a host shell. |
| X2 | "`respx` or `pytest-httpx`": `testing.md:62`, `python.md:95`, `PythonDataService/CLAUDE.md:141`, `.claude/CLAUDE.md:176` | untrue (partial) | Only `respx` is installed (`requirements-dev.txt:20`; `:21` notes neither intercepts alpaca-py); 0 tests use `httpx_mock`. |
| X3 | "NSubstitute or Moq": `testing.md:52`, `dotnet.md:54`, `.claude/CLAUDE.md:164`, `:167` | untrue (partial) | `Backend.Tests/Backend.Tests.csproj` references Moq only. |

### Repo skills (`.claude/skills/`)

| # | Item | Kind | Evidence |
|---|---|---|---|
| S1 | `auto-research-tick/` | whole dead skill | `docs/audits/auto-research/state.json` has sat in `build-alpha-validation-complete-awaiting-review` since its last run (2026-05-12). That mode only prints a report under `docs/audits/auto-research/runs/`, which was deleted 2026-07-04 (`8441f4f6`; the directory is absent). `hardened-nightly` is "Not implemented yet" (`SKILL.md:21`). No workflow or scheduled task invokes it. |
| S2 | `grill-me/`, `grill-with-docs/`, `handoff/`, `improve-codebase-architecture/`, `tdd/` | inert (shadowed) | Fact 2. Also `grill-me/SKILL.md:7` and `grill-with-docs/SKILL.md:7` call a `grilling` skill that exists only in `~/.codex/skills/` — broken for Claude. |
| S3 | `setup-matt-pocock-skills/` | inert (shadowed) / never run | Fact 2 (`~/.claude/skills/setup-matt-pocock-skills`); its output `docs/agents/` was never created. |
| S4 | `diagnosing-bugs/` vs personal `~/.claude/skills/diagnose/` | inert (duplicate) | Both are listed this session and both trigger on "diagnose / debug this" with the same reproduce → minimise → hypothesise → instrument → fix → regression-test loop. Keep one (owner question 1). |
| S5 | `wayfinder/SKILL.md:79`, `:111` and `improve-codebase-architecture/SKILL.md:64` "Call the Skill tool … for `grilling`" | untrue | There is no `grilling` skill in the repo or `~/.claude/skills/`; the map routes around it ("Grilling tickets: `grill-me` + `domain-modeling`", #2700 Notes). |
| S6 | `domain-modeling/SKILL.md:12-40` `docs/adr/` layout and "If no `docs/adr/` exists, create it" | untrue | This repo's 67 ADRs live in `docs/architecture/adrs/`; `docs/adr/` is absent, so the line would fork the ADR home. (Same in the shadowed `improve-codebase-architecture/SKILL.md:14`, `:25`.) |
| S7 | `code-review/SKILL.md:13`, `:29` `docs/agents/issue-tracker.md` / "run `/setup-matt-pocock-skills`" | untrue | `docs/agents/` does not exist; the tracker is GitHub via `gh`. |
| S8 | `implement/SKILL.md:11` "the full test suite once at the end" | retired | Full local runs are retired (`testing.md:91-97`; owner 2026-09-30: lint + the proving test only, CI is the gate). |
| S9 | `port-indicator/SKILL.md:57-64` divergence buckets | inert | A 7-bucket copy of `reconcile-backtest/SKILL.md:25-36` (canonical). |
| S10 | `port-indicator/SKILL.md:67-70` Phase 5 steps 1–2 and `:80` "`docs/references/` note" | retired | As C1. `:71` (keep vendored reference) stays. |
| S11 | `port-indicator/SKILL.md:31` "use the GitHub MCP", `:39` "via the Postgres MCP" | untrue | Fact 4. |
| S12 | `extract-math-from-paper/SKILL.md:25` "Use the `pdf-reading` skill" | untrue | No `pdf-reading` skill in the repo or `~/.claude/skills/`. |
| S13 | `extract-math-from-paper/SKILL.md:66-71` Phase 4 "Create `docs/references/<method-name>.md`" | retired | As C1. |
| S14 | `reconcile-backtest/SKILL.md:76` "Add an entry to `docs/references/reconciliations/…`" | retired | As R8. `:77` (encode the tolerance in the test) stays. |
| S15 | `learn-ai-validation/SKILL.md:90-127` "Inline critical rules" | inert | Copies of the always-loaded rule files (fact 1). The "Desktop-portable" reason does not apply: Claude Desktop does not load repo skills either. Inside it, `:118`, `:126` are untrue (see S16). |
| S16 | `learn-ai-validation/SKILL.md:118`, `:126`, `:164`, `:167` (math belongs in Python; no .NET/Frontend math without a "Python canonical") | untrue | Contradict `CLAUDE.md:22` and the skill's own `:80` ("Math may live in Python, .NET, Frontend…"). |
| S17 | `learn-ai-validation/SKILL.md:151` "`numerical-rigor.md` — tolerances, fixtures, timestamps…" | untrue | Timestamps moved to `temporal-rigor.md`, which this list omits. |
| S18 | `learn-ai-validation/SKILL.md:155` per-port notes "(currently empty…)" | untrue / retired | `docs/references/` holds 82 entries; per-port notes are retired paperwork. |
| S19 | `write-graphql-resolver/SKILL.md:25-57` static `[QueryType]` class pattern, `:111` tests in `Backend.Tests/GraphQL/` via `IRequestExecutor` | untrue | As R15; `Backend.Tests/GraphQL/` does not exist and 0 tests use `IRequestExecutor`. |
| S20 | `build-angular-component/SKILL.md:3`, `:8`, `:23`, `:91`, `:113`, `:140` "Angular 21" / "v21" | untrue | `Frontend/package.json:32` `^22.0.0`. |
| S21 | `build-angular-component/SKILL.md:19` "Read `Frontend/.claude/rules/angular.md` if present…" and `:23-37` conventions | inert | No such file; the root rule is already loaded (fact 1); `:23-37` duplicates `angular.md:9-18`. |
| S22 | `add-fastapi-endpoint/SKILL.md:99` "Angular frontend (rare): usually goes through the .NET GraphQL gateway" | untrue | 33 non-spec Frontend files call `/api/` (the data plane) directly vs 27 using GraphQL; `Frontend/proxy.conf.js:162-167` routes `/api`. |
| S23 | `add-fastapi-endpoint/SKILL.md:77-78`, `:85`, `:89` example paths | untrue | `app/services/indicator_service.py` and `tests/unit/services/` don't exist. |
| S24 | `meta-propose-skill/SKILL.md:66-76` "Skill authoring rules" | inert | Duplicates `writing-for-agents/` (whose description triggers on creating or editing skills) and its `SKILL-MECHANICS.md`. `:70`'s gerund-naming rule doesn't match most repo skill names. |

### Commands (`.claude/commands/`)

| # | Item | Kind | Evidence |
|---|---|---|---|
| K1 | `test-all.md` | whole command, retired | Runs every gate suite locally, including the unsharded frontend suite that "can OOM" (`Frontend/CLAUDE.md:64`); local runs are targeted only (`testing.md:91-97`); its .NET step cannot run on the host (X1). The directory must keep a file — see Hazard 1. |

`lint-all.md` (minus X1) and `health-check.md` stay.

## What the cuts orphan

- **`numerical-rigor.md:76-80` stub is a redirect, not sediment.** At least 14 files cite
  "`numerical-rigor.md` § Timestamp rigor", e.g. `app/research/ml/artifact.py:8`,
  `app/research/runs/result.py:4`, `app/routers/spec_strategy.py:73` (copied into the
  generated `Frontend/src/app/api/broker.types.ts:25559`), `app/lean_sidecar/manifest.py:10`,
  `app/engine/strategy/spec/tests/test_spec_router.py:151`,
  `docs/architecture/lean-sidecar-lab.md:81`, `docs/architecture/edge-feature-design.md:81`,
  ADR 0022 `:9`. `app/services/dataset_service.py:43` cites a section ("External-API
  ingestion") that is already gone.
- **`.claude/CLAUDE.md`** is linked from `docs/indicator-reliability-methodology.md:1367` and
  its served copy `Frontend/src/assets/docs/indicator-reliability-methodology.md:1367`,
  cited in a comment at `app/routers/lean_sidecar.py:948`, and required by
  `scripts/check_documentation_contract.py:20`.
- **Docs that lose their only rule-level live link:**
  - `docs/references/**` and `docs/references/reconciliations/**` — once C1, R7–R9, R5
    and S10/S13/S14/S18 go, nothing in the rules sends a reader there → #2714 decides
    which notes still have a live link.
  - `docs/audits/computational-fidelity-2026-04-22-addendum.md` — cited only by A4 →
    #2711.
  - `docs/audits/auto-research/*` (`state.json`, `baseline-math-rigor.md`,
    `build-alpha-functionality-validation.md`, `findings/`) — exist only for S1 → #2711.
    `docs/known-gaps.md:5-9` mentions the ledger → #2713.
- **Skills that point at cut rules or skills:**
  - `code-review/SKILL.md:13` points at `setup-matt-pocock-skills` (S3); cut S3 and S7
    together.
  - `learn-ai-validation/SKILL.md:155`, `port-indicator:69-70`,
    `extract-math-from-paper:70` and `reconcile-backtest:76` all carry the retired
    paperwork rule; cut them with C1/R8.
  - `CLAUDE.md:81` names the personal `thermo-nuclear-code-quality-review` skill (outside
    the repo): its scope changes, the skill stays.
- **Already-dangling links found on the way** (not caused by these cuts):
  - `docs/architecture/research-artifact-seam.md:165` →
    `.claude/skills/improve-codebase-architecture/LANGUAGE.md` (no such file) → #2712.
  - `docs/superpowers/specs/2026-05-15-readme-auto-updater-design.md` →
    `.claude/skills/auto-readme-tick` (no such skill) → #2711.
- **Message text only (no pin):**
  `tests/contracts/test_pre_commit_lint_gate_parity.py:5-6`, `:38` name the lint commands
  in `.claude/CLAUDE.md` and `PythonDataService/CLAUDE.md` → #2730 if those lines move.

## Hazards the cutting PR must carry

1. **The doc-contract checker pins this area.** `scripts/check_documentation_contract.py`:
   - `:18-25` requires `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules`,
     `.claude/skills`, `.claude/settings.json`, `.claude/commands` and `.claude/hooks` to
     exist;
   - `:186` requires the `## Codex and Claude compatibility` heading in `AGENTS.md`;
   - `:236-256` checks every `Angular N` / `.NET N` mentioned in `AGENTS.md` against the
     manifests;
   - `:149-176` resolves `.claude/…` links in canonical docs.

   So deleting `.claude/CLAUDE.md` outright (D2 leaves `:1-43`) or emptying
   `.claude/commands/` fails `tests/contracts/test_documentation_contract.py::test_validate_repository_current_repository_passes`.
   Either keep the path or edit the checker in the same PR.
2. **A served doc pair.** Retargeting the `.claude/CLAUDE.md` link means editing both
   `docs/indicator-reliability-methodology.md` and its `Frontend/src/assets/docs/` copy
   (`SERVED_DOCUMENT_COPIES` in the same checker).
3. **Contract regen.** Retargeting the stub reference in
   `app/routers/spec_strategy.py:73` changes the OpenAPI snapshot and the generated
   `broker.types.ts`, so that PR joins the serial contract-regen merge queue.
4. **Write protection.** `.claude/skills` and `.claude/hooks` are write-protected in the
   agent sandbox; the cutting PR edits them unsandboxed.
5. **Sessions load the main checkout's rules.** Claude Code reads `.claude/rules` and the
   `CLAUDE.md` files from `/Users/inkant/learn-ai`, so cuts take effect only after merge
   and a pull there. Never switch branches in that checkout — live clerks bind-mount it.
6. **Order against the rule rewrite.** The locked rules are not in effect yet. Cutting C7/C9/R2/A8
   (regression test, thermo) before the rewrite lands removes a gate with no scoped
   replacement. Land the retired rows with, or after, the "Rule rewrite" text.
7. **Shadowed skills.** Removing S2/S3 changes nothing on the owner's Mac but removes those
   skills from any machine without personal copies (the Windows migration, #2272).
   Settle owner question 1 first.
8. **Codex parity.** `AGENTS.md` mirrors many `CLAUDE.md` lines (C1/A3, C7/A8, C2/A6).
   Cut both sides in the same PR so Codex and Claude don't diverge further (A4 is what
   that drift already looks like).
9. **Kill lists age.** Re-check every row's evidence at the cutting PR's own SHA.

## Pointers outside this area (not rows)

- `.claude/settings.json:3-26` `mcpServers` block is never read (fact 4), and
  `references/MCP-SETUP.md` is missing → #2716.
- `.claude/hooks/post-edit-validate.sh` is registered in no settings file: there is no
  `hooks` key in `.claude/settings.json`, `.claude/settings.local.json` or
  `~/.claude/settings.json` → #2716. Hazard 1 still requires `.claude/hooks/` to exist.
- `docs/audits/auto-research/*`, `docs/audits/computational-fidelity-2026-04-22-addendum.md`,
  `docs/superpowers/specs/2026-05-15-readme-auto-updater-design.md` → #2711.
- `docs/references/**`, `docs/references/reconciliations/**` → #2714.
- `docs/architecture/research-artifact-seam.md:165` dangling link → #2712.
- `docs/known-gaps.md:5-9` auto-research ledger mention → #2713.

## Checked and kept (for the reviewer, not rows)

- **Sacred:**
  - the provider boundary, `CLAUDE.md:5-14` and `AGENTS.md:5-19`;
  - `numerical-rigor.md` equivalence levels, tolerances, fixture lifecycle, warmup,
    floating-point hygiene, the code-backed trade-level taxonomy
    (`app/research/parity/qc_reconciler.py:59-69`) and sovereignty;
  - `temporal-rigor.md` apart from R11/R12 (`MAX_TIMESTAMP_MS`, `build_csv_bytes`, the
    calendar module's ten functions, the single `mcal.get_calendar` at
    `app/lean_sidecar/trading_calendar.py:30`, the shared timestamp display component —
    all verified).
- **Verified true:**
  - `angular.md:34-59` (every named selector and service exists);
  - the `Frontend/CLAUDE.md` Gotchas;
  - the Backend Gotchas;
  - the Polygon 5-year note in `PythonDataService/CLAUDE.md:147`;
  - the `CLAUDE.md:76-78`, `:83-85` hard rules.
- **Left for the rule rewrite** (open on the map, so no rows here):
  - the `learn-ai-validation` provenance block and registry workflow (`:12-80`,
    `:129-138`), and the `AGENTS.md:112-115` "update both registries in the same PR"
    mandate;
  - `dotnet.md` prescriptions that no Backend code uses yet — union result types (`:34`),
    DataLoader (`:33`), `IDbContextFactory` (`:43`), `IRequestExecutor` tests (`:57`);
    0 uses each;
  - `testing.md:78-112` (the 120-second budget and local test policy) — CI-shape fog;
  - `diagnosing-bugs` Phase 5's regression test, which is conditional on a seam rather
    than mandatory.
- **Two taxonomies are two levels.** `reconcile-backtest`'s root-cause buckets and
  `numerical-rigor`'s trade-level categories don't conflict; only the `port-indicator`
  copy goes (S9).

## Not reviewed

- **Bodies grep-checked only, not read line by line:**
  - `codebase-design/` (`SKILL.md`, `DEEPENING.md`, `DESIGN-IT-TWICE.md`);
  - `diagnosing-bugs/` body and `scripts/hitl-loop.template.sh`;
  - `domain-modeling/ADR-FORMAT.md`, `CONTEXT-FORMAT.md`;
  - `tdd/mocking.md`, `tdd/tests.md`;
  - `improve-codebase-architecture/HTML-REPORT.md`;
  - `setup-matt-pocock-skills/` templates;
  - `wayfinder/` body;
  - `auto-research-tick/` body past its dispatch table (moot under S1);
  - `write-graphql-resolver/SKILL.md:96-161`, `build-angular-component/SKILL.md:39-163`,
    `add-fastapi-endpoint/SKILL.md:26-72`;
  - the 16 `agents/openai.yaml` metadata files.

  The grep covered repo-specific pointers: versions, paths, `CONTEXT.md`, `docs/adr`,
  `docs/agents`, `grilling`, MCPs and retired-rule phrases.
- **Can't verify from here:** `AGENTS.md:163-165` (the containerized Codex publishing
  environment).
- **Outside this area:** `.claude/launch.json`, and `.claude/settings.json` beyond its
  MCP and hook keys.
- **Model-relative:** C11 was judged against this session's harness prompt, not by
  running the document.
