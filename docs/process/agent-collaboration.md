# Agent collaboration contract

How Claude (developer) and Codex (reviewer) co-develop this repo, with Tim as admin / mediator / merge authority.

This is the single source of truth for the workflow. Both agents should load this file at the start of any non-trivial task. If practice drifts from this doc, update the doc — don't relitigate the contract per task.

## Roles

**Claude — implementation lead**
- First-pass implementation: features, fixes, tests, docs.
- Holds repo-wide invariants in mind across multi-file edits (timestamp ms-UTC, golden fixtures, layered authority).
- Avoids broad refactors without explicit approval.
- Failure modes to guard against: over-explaining, adding comments/docstrings the rules forbid, optimistic numerical claims without re-running fixtures, "cleanup while here."

**Codex — review and verification lead**
- Reviews diffs against repo rules and the task's review rubric.
- Strong on local correctness, minimality, mock/runtime mismatches, and "receipts" (file paths, test output, exact rule citations).
- Does **not** auto-load `.claude/rules/*.md`; must be told which files to load per review.
- Guards specifically: math authority drift (duplicate numerical implementations without a named canonical owner, provenance block, or parity test), ISO timestamps escaping wire/storage, "looks right" ports without fixtures, silent exception handling, dependency creep, broad edits mixed into focused fixes.

**Tim — admin / mediator / merge authority**
- Sets task and constraints.
- Merges Claude's plan + Codex's rubric into the approved plan before implementation begins.
- Arbitrates conflicts. Decides scope, priority, and when to merge.

## Division of labor in this repo

Sharper than generic dev/review because the repo has scientific constraints.

**Claude leads:**
- Angular UI work
- Backend GraphQL plumbing (Hot Chocolate v15)
- FastAPI endpoint scaffolding
- Straightforward bug fixes
- Documentation drafts
- Cross-stack feature integration along existing paths

**Codex reviews especially hard:**
- Any numerical computation, regardless of layer
- Any duplicate numerical implementation — must name the canonical owner, carry a provenance block, and ship a parity test (`learn-ai-validation` skill encodes the rule, per `CLAUDE.md` rule 5)
- Timestamp formats and boundary serialization
- Backtest, fill, commission, P&L, signal, indicator, statistics, Greeks, volatility logic
- Any change that introduces or moves a math authority path
- Any new dependency
- Any test that claims parity or equivalence

**For math ports specifically**, Codex must require all of:
- Reference source identified (repo + commit, paper + section, or vendored under `references/`)
- Provenance block on the implementation (`Formula` / `Reference` / `Canonical implementation` / `Validated against`) — `learn-ai-validation` skill enforces this
- Golden fixture present at `PythonDataService/tests/fixtures/golden/<name>/` (or the canonical owner's equivalent fixtures path if the math lives in another layer)
- Tolerance stated and justified per the category-specific defaults in `.claude/rules/numerical-rigor.md` § "Tolerance rules → Default tolerances" (indicators, accumulated PnL, options Greeks, probabilities each have different defaults; looser values require classifying the divergence first per the same rule)
- `docs/references/<name>.md` updated
- `docs/math-sources-of-truth.md` updated if authority changes
- A parity test asserting equivalence with explicit tolerances; if the implementation is a duplicate, the test names the canonical file

## The loop

```
1. Tim: task + constraints                     → both agents
2. Claude: implementation plan
   Codex:  review rubric (with rules loaded)   → in parallel
3. Tim: merge into the approved plan
4. Claude: implement on feat/* or fix/* branch
5. Codex: review against the approved rubric
6. Claude: fix blocking issues (one round)
7. Tim: merge, or defer follow-ups to a new task
```

**One revision round cap.** Second-order issues become follow-up tasks unless Tim explicitly reopens the loop. Non-blocking nits get a one-line "won't fix, here's why" or a follow-up issue.

## Handoff templates

### Claude's implementation handoff

```
## Plan
<the merged-and-approved plan, restated>

## Rules to load  ← Codex pastes these into its review prompt
- .claude/rules/<file>.md (why it's relevant)
- docs/references/<port>.md (existing tolerance / authority note, if applicable)

## Diff
<branch name + commit SHAs, or unified diff>

## Claims
- Tests added: <files>
- Tests run: <command + result>
- Tolerances used: <fixture path + atol/rtol>
- Fixtures touched: <paths, or "none">

## Risks
<where Codex should look hardest — name files and concerns>

## Out-of-scope
<what was deliberately not changed, even if related>
```

### Codex's review output

```
## Verdict
block | nit-only | approve

## Blocking
file:line — rule/source — issue — required fix

## Non-blocking
file:line — suggestion — why it matters

## Verified
<commands or tests inspected/run, with results>

## Missed-by-rubric
<advisory only — instinct findings outside the agreed rubric>
```

If Codex cannot cite a rule, source, test, or concrete behavior, the finding belongs under `Missed-by-rubric`, not `Blocking`.

## Conflict resolution

Resolve in this order; escalate only when lower levels are silent:

1. Vendored references in `references/` (ground truth for ports).
2. Official docs (angular.dev, learn.microsoft.com, chillicream, fastapi, pandas).
3. `.claude/rules/*.md` and `CLAUDE.md` files.
4. Whichever agent cites a concrete file path / commit / test result wins over whoever argues from "best practice."
5. Tim decides.

By disagreement type:
- **Product behavior** → Tim.
- **Math correctness** → require evidence: fixture, tolerance, test result, reference citation.
- **Style** → existing repo patterns win. No reformatting unrelated code.
- **Architecture** → require a short ADR-style note in `docs/architecture/`.
- **Scope** → smaller change wins unless Tim explicitly approves expansion.

## Branching

- Claude works on `feat/<topic>` or `fix/<topic>`.
- Codex does not commit directly to Claude's branch unless Tim asks.
- If Codex contributes a review-cleanup patch, use `codex/review-fix-<topic>` and merge / cherry-pick — keep authorship and intent legible in `git log`.

## Hard boundaries Codex follows as reviewer

- No requests to restyle unrelated code.
- No comment/docstring requests unless a repo rule or genuine readability risk supports it.
- Math claims are unproven until fixture path, tolerance, and test result are present.
- Timestamp boundary drift is high priority by default.
- Generic best-practice opinions stay advisory (`Missed-by-rubric`).
- Respect the one-revision-round cap unless Tim reopens the loop.

## Things to watch (composite list from both agents)

- Math authority drift — duplicate numerical implementations without a named canonical owner, provenance block, or parity test. (Math may live in any layer; what's not negotiable is one canonical implementation per concept.)
- ISO strings, `DateTime`, or naive `datetime` escaping into wire/storage boundaries (see `numerical-rigor.md` ban list).
- Tests that only check shape, not numerical behavior.
- "Looks right" ports without fixtures.
- Silent exception handling (`catch {}`, `except: pass`).
- Hidden dependency creep.
- Mocks that don't match real SDK objects (runtime/test divergence).
- Docs not updated when authority moves.
- Broad edits mixed into focused fixes.
- `console.log`, `print`, `Console.WriteLine` left in code.

## When to skip the loop

Small, low-risk changes — typo fixes, one-line bug fixes, dependency version bumps with no API change — can go straight from Claude to Tim without a Codex review round. Use judgment; if in doubt, run the loop.

## Updating this contract

Changes to this file go through the same loop (Claude proposes diff, Codex reviews, Tim merges). Don't bypass it for the document that defines the bypass rules.
