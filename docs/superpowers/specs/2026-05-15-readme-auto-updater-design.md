# README auto-updater — design

**Date:** 2026-05-15
**Status:** design, pre-implementation
**Owner:** Tim
**Driver commit:** README.md at `e71e1137` (host runner daemon UI), which is already drifting from the live-trading work merged after it

---

## 0. Problem & goal

The repo's `README.md` is comprehensive but stops at research / backtesting features. The recent live-trading work — host runner daemon, broker paper-run observer UI, IBKR integration, indicator-state persistence, recovery-flatten — isn't there. The README will keep drifting from reality unless someone owns sync.

**Goal:** A bounded, human-gated automation that keeps the Features section of `README.md` in sync with the actual code, triggered on a hybrid schedule (weekly cron + manual `/auto-readme-tick`). First invocation doubles as the initial modernization pass.

**Non-goals:**

- Rewriting non-Features sections of the README (Getting Started, GraphQL sample, etc.)
- Replacing human content judgment — every change ships as a PR for human review
- A general-purpose docs maintainer — the scope is one fenced block in `README.md`, nothing else

## 1. Architecture & file layout

A new skill `auto-readme-tick` modeled on the existing `auto-research-tick`. Files added:

```
.claude/skills/auto-readme-tick/SKILL.md      # skill prose, ~100 lines
docs/audits/auto-readme/
  state.json                                  # last_processed_sha, last_run_at, schema_version
  runs/YYYY-MM-DD.md                          # one run summary per tick
  dry-runs/YYYY-MM-DD-<shortsha>.md           # proposed README (dry-run mode)
  dry-runs/YYYY-MM-DD-<shortsha>.diff         # unified diff (dry-run mode)
README.md                                     # gains a fenced block (see §2.1)
```

Triggers:

- **Manual:** user invokes `/auto-readme-tick` or "run the readme tick"
- **Scheduled:** weekly cron (Sunday 03:00 local) registered via the `schedule` skill

Strict scope: the skill may write only to `README.md` (inside the fence), `docs/audits/auto-readme/**`, and `.git/` indirectly (branch + push). All other paths are read-only.

## 2. Components

### 2.1 The fence

Two HTML comments delimit the auto-managed region of `README.md`:

```markdown
<!-- AUTO-UPDATED:FEATURES — managed by .claude/skills/auto-readme-tick. Edits between the fences will be overwritten. -->
... features content ...
<!-- /AUTO-UPDATED:FEATURES -->
```

Rules:

- Exactly one opening marker and one closing marker, on their own lines.
- The skill refuses to run if either marker is missing or duplicated.
- Outside the fence is hand-edited and never touched by the skill.
- The fence wraps the existing `## Features` section. The section heading itself sits *inside* the fence so the skill can restructure subsections.

### 2.2 State file

`docs/audits/auto-readme/state.json`:

```json
{
  "schema_version": 1,
  "last_processed_sha": "e71e1137",
  "last_run_at": "2026-05-15T22:30:00-05:00",
  "last_run_status": "no-changes | pr-opened | error | dry-run | budget-exceeded | malformed-generation",
  "last_pr_url": null,
  "runs": [
    {
      "date": "2026-05-15",
      "sha_range": "abc..def",
      "feat_commits": 3,
      "files_read": 47,
      "status": "pr-opened",
      "pr": "https://github.com/tim1016/learn-ai/pull/..."
    }
  ]
}
```

Atomic write: `state.json.tmp` → rename. Persist after every state transition and at least every 10 minutes during long runs.

`last_processed_sha = null` means bootstrap mode — see §3.2.

### 2.3 The skill

`auto-readme-tick` SKILL.md drives one tick:

1. Read `state.json`. Refuse on schema mismatch.
2. Run pre-flight refusals (§4.1). Exit cleanly on any failure.
3. `git fetch origin master`.
4. `git log --grep='^feat' <last_processed_sha>..origin/master --pretty=%H` → commit list.
5. If empty: status `no-changes`, advance `last_processed_sha` to `origin/master` HEAD, write run summary, exit.
6. For each commit: `git show --name-only --pretty=format: <sha>` → union of touched files.
7. Filter file list — skip lockfiles, vendored references, fixtures, generated, binaries (§3.3).
8. Read current contents of remaining files at `origin/master`.
9. Read `README.md`, locate fence, extract current Features section.
10. Single Claude call: generate updated Features section. Prompt rule: "Integrate new capabilities, preserve existing well-written entries verbatim where still accurate, remove entries for code that no longer exists. Output the new section only — no commentary."
11. Validate output (§4.3). On any validation failure: discard, log as `malformed-generation`, do not advance sha, exit.
12. Diff old vs new fence content. If byte-identical: status `no-changes`, advance sha, exit.
13. Create branch `readme/auto-update-YYYY-MM-DD-<shortsha>`, replace fence content in README, commit with message `docs(readme): auto-sync features (N feat commits since <sha>)`, push, open PR (§3.4).
14. Update state.json: advance `last_processed_sha`, set `last_pr_url`, status `pr-opened`.
15. Write `runs/YYYY-MM-DD.md`.

### 2.4 Cron registration

A one-time `schedule` skill invocation registers a weekly cron firing `auto-readme-tick`. No separate runner — the cron is just a wakeup to invoke this skill.

## 3. Data flow

### 3.1 Normal tick

```
Read state ─► git fetch ─► commit scan ─► file list ─► filter ─► read files
   │
   └─► read fence ─► Claude generation ─► validate ─► diff
                                                       │
                          no-change                    │ change
                                                       ▼
                          advance sha         create branch ─► push ─► gh pr create
                          status=no-changes   advance sha ─► state.json ─► run summary
                          run summary
```

### 3.2 Bootstrap (first run)

`last_processed_sha == null`:

1. `git log -1 --format=%H -- README.md` → SHA of last README commit
2. Use that as `last_processed_sha` and continue the normal flow

This is how Approach 3's first invocation *is* the initial modernization pass — no special-case code. The skill processes every `feat(...)` commit since the README was last touched and opens one PR with the full backlog.

### 3.3 File filter

Skip files matching any of:

- `package-lock.json`, `*.csproj.lscache`, `*.csproj.user`
- `dist/**`, `node_modules/**`, `bin/**`, `obj/**`
- `**/*.snap`, `**/*.min.*`
- `references/**` (vendored reference code)
- `PythonDataService/tests/fixtures/**` (golden fixtures, not feature-bearing)
- Non-text files: extension allow-list (`.ts`, `.html`, `.scss`, `.cs`, `.py`, `.md`, `.json`, `.yaml`, `.yml`, `.sh`, `.ps1`, `.toml`, `.txt`)

Everything else is read. `package.json` and similar config-shaped files are included; the LLM judges whether the change is feature-relevant.

### 3.4 PR body template

```markdown
## Auto-sync of README Features section

Triggered by N `feat(...)` commits merged to master since the last README sync.

### Commits processed

- `<sha>` — <subject>
- ...

### Stats

- Fence diff: +X / -Y lines
- Files read: F
- Run status: pr-opened / high-churn (if >80% rewrite)

### Reviewer checklist

- [ ] Fence diff describes real shipped features, not aspirational
- [ ] No fabricated capabilities
- [ ] No content outside the fence was touched
- [ ] Deleted entries correspond to actually-removed code

### State

- last_processed_sha: <old> → <new>
- PR opened by: auto-readme-tick @ <ISO timestamp>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

## 4. Error handling & safety

### 4.1 Pre-flight refusals

| Condition | Behavior |
|-----------|----------|
| `state.json` malformed or schema mismatch | Refuse. Tell user to fix or delete (and re-bootstrap). |
| Fence markers missing or duplicated in README | Refuse. Tell user to add the fence before first run. |
| Working tree dirty | Refuse. Won't push a branch over uncommitted user work. |
| On master with unpushed commits | Refuse. |
| Prior auto-readme PR still open | Refuse — no stacked auto-PRs. List the open PR's URL. |
| `git fetch origin master` fails | Refuse, surface error. |
| `gh` not authenticated | Refuse, surface auth instruction. |

### 4.2 Scope guards (during tick)

- Skill may only write to: `README.md` (inside fence), `docs/audits/auto-readme/**`, `.git/` (via branch + push).
- Any attempted write to another path aborts the tick, writes a run summary noting the violation, exits non-zero.
- No new dependencies, no package installs, no container starts.
- No `git push --force`, no `--force-with-lease`, no rebase.
- Branch name pattern `readme/auto-update-YYYY-MM-DD-<shortsha>` is unique per tick.

### 4.3 Generation-time validation

- If output doesn't contain both fence markers in the new README: discard, log as `malformed-generation`, do not advance sha.
- If diff >80% of fence size: still open PR, tag PR body with `⚠ large-rewrite`.
- If new fence content <200 chars (suspiciously empty): refuse, log, exit.

### 4.4 Budget & resume semantics

- **Hard cap: 4 hours wall-clock** per tick, or until API usage runs out.
- **Soft checkpoint every ~10 min:** persist state and partial run summary.
- **Budget exhausted during file-read or generation:** do **not** generate / do **not** open PR / do **not** advance sha. Status `budget-exceeded`. Next tick retries from the same sha with fresh budget.
- **Budget exhausted after PR is opened:** state already advanced, work durable. Run summary completes the next time the skill runs.
- **No partial PRs.** Either the PR is complete and accurate, or there is no PR.

### 4.5 Failure recovery

| Failure | Recovery |
|---------|----------|
| Push fails (network) | State not advanced. Next tick retries. |
| PR creation fails | Delete local branch. State unchanged. Next tick retries. |
| Skill killed mid-run | Atomic state writes; resume from last persisted sha. |
| Bad PR opened (hallucination, wrong scope) | Close the PR. Optionally revert `state.json` to prior `last_processed_sha` (one entry back in `runs[]`). Next tick reprocesses cleanly. |
| User pushes manual edits to the fence | Documented as "fence is auto-managed". The next generation preserves them only if Claude judges them still accurate. |

### 4.6 What the skill never does

- Touch any code outside `README.md`'s fence and `docs/audits/auto-readme/**`
- Open issues, comment on existing PRs, post to Slack/email
- Re-open closed auto-readme PRs
- Delete or rename the fence markers
- Modify `docs/architecture/`, `docs/references/`, any other docs

## 5. Validation & testing

### 5.1 Dry-run mode (baked into skill)

Invoked as `/auto-readme-tick dry-run`:

- Runs steps 1–11 normally (read state, scan, read, generate, validate)
- **Skips:** branch creation, push, PR, state advancement
- **Writes:** proposed README to `docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.md` and unified diff to `.../YYYY-MM-DD-<shortsha>.diff`
- User reviews. If happy, run live. If not, edit fence by hand or tune skill prompt.

The option-A initial modernization pass uses dry-run first.

### 5.2 Skill-introduction PR validation

Before merging the PR that *introduces* this skill:

- [ ] Fence added to `README.md`, both markers present, regex-matchable
- [ ] `docs/audits/auto-readme/state.json` exists with `last_processed_sha: null`
- [ ] `/auto-readme-tick dry-run` runs end-to-end on current master and produces a sensible diff
- [ ] Pre-flight refusals fire correctly when bad state is set up by hand (dirty tree, missing fence, malformed state.json)

### 5.3 Per-tick PR review

Every PR opened by the skill is gated by human review using the checklist in the PR body (§3.4).

### 5.4 Rollback

If a tick opens a bad PR: close the PR, optionally revert `state.json` one entry, next tick reprocesses cleanly. No special tooling.

## 6. Trade-offs accepted

- **Commit-message dependency for *which commits to look at*.** A PR landing as `chore:` instead of `feat:` will be skipped. Mitigation: PR title and conventional-commit hygiene are already part of this repo's workflow (see `MEMORY.md` → PR workflow). The skill reads the *files*, not the commit messages, so a mistyped `chore:` that touches feature files is still a one-PR delay (caught next time a `feat:` lands and pulls those files into the read set).
- **Single fence, not multiple.** Easier to reason about and to reject malformed generations. If the auto-managed surface needs to grow (e.g., Project Structure also rots), add a second fence with a separate skill rather than expand this one.
- **PR-only, no direct commit to master.** Slight ceremony cost per update; in exchange, every change is reviewable and reversible.
- **Weekly cron, not per-merge.** README can be up to 7 days stale. Acceptable for a research repo; reduce cron interval if it becomes a real friction point.

## 7. Open questions for the implementation plan

- Exact shell command set for the bootstrap SHA-discovery — `git log -1 --format=%H -- README.md` works in normal cases but may need a tweak if README has been renamed historically.
- Whether the cron registration is part of this PR or a follow-up (`schedule` skill should be idempotent, so safe to include).
- Initial fence content: do we ship the fence empty and let the bootstrap tick fill it, or ship it pre-populated with the current `## Features` content? Latter is gentler — recommended.
