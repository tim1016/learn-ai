# README auto-updater implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a bounded, human-gated automation that keeps the Features section of `README.md` in sync with code, via a hybrid trigger (weekly cron + manual `/auto-readme-tick` skill) producing PR-only output through a fenced auto-managed block.

**Architecture:** Approach 3 from the spec — `git log --grep=^feat` provides the "where to look" signal, Claude reads the *current* state of the touched files (not the historical diff) and rewrites the fenced block. Single skill prose file, state file under `docs/audits/auto-readme/`, atomic writes, hard 4-hour budget per tick.

**Tech Stack:** Claude Code skill (prose only), `git`, `gh` CLI for PR creation. No new dependencies in any stack.

**Spec:** `docs/superpowers/specs/2026-05-15-readme-auto-updater-design.md`

**Branch:** `feat/readme-auto-updater` (already created, spec already committed at `f49294f`)

**Out of scope for this PR:** The first auto-generated modernization PR (PR-B). PR-B is produced by running the skill after this PR (PR-A) merges. See "Post-merge steps" at the end of this plan.

---

## File structure

Files this plan creates or modifies:

```
README.md                                          # MODIFY: add fence around existing ## Features
docs/audits/auto-readme/state.json                 # CREATE: bootstrap-ready state file
docs/audits/auto-readme/README.md                  # CREATE: explain the directory
.claude/skills/auto-readme-tick/SKILL.md           # CREATE: the skill prose
docs/superpowers/plans/2026-05-15-readme-auto-updater.md  # this file (already being written)
```

No source code files, no tests, no new dependencies.

---

## Task 1: Add the fence to `README.md`

The fence wraps the existing `## Features` section as-is — no content change. This is what the spec calls "ship pre-populated".

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read current `## Features` boundaries**

Locate the `## Features` heading and the next `## ` heading after it (which marks where Features ends). In current master, Features starts at line 31 (`## Features`) and the next top-level section is `## Data Pipeline` around line 273.

Run: `grep -n '^## ' README.md`

Expected output (line numbers will vary slightly):
```
5:## Architecture
31:## Features
273:## Data Pipeline
301:## Database Schema
...
```

- [ ] **Step 2: Wrap the Features section in the fence**

Insert the opening fence marker on a line of its own *immediately before* `## Features`, and the closing fence marker on a line of its own *immediately after* the last line of the Features section (the blank line before `---` separator above `## Data Pipeline`).

Opening fence (exact bytes):
```
<!-- AUTO-UPDATED:FEATURES — managed by .claude/skills/auto-readme-tick. Edits between the fences will be overwritten. -->
```

Closing fence (exact bytes):
```
<!-- /AUTO-UPDATED:FEATURES -->
```

The fence wraps everything from `## Features` (inclusive) through the last paragraph of Features (currently ending at "...with TradingView mini-chart widgets, aggregate counts, date ranges, and data sanitization summaries.").

Do not modify the Features content itself.

- [ ] **Step 3: Verify the fence parses**

Run: `grep -c '<!-- AUTO-UPDATED:FEATURES' README.md && grep -c '<!-- /AUTO-UPDATED:FEATURES -->' README.md`

Expected: both commands print `1`. If either prints `0` or `>1`, the fence is malformed — fix before continuing.

- [ ] **Step 4: Verify the fence's content boundaries**

Run: `awk '/<!-- AUTO-UPDATED:FEATURES/,/<!-- \/AUTO-UPDATED:FEATURES -->/' README.md | head -5`

Expected: opening fence comment, then `## Features`, then the first lines of the Features intro paragraph. If `## Features` isn't the first non-fence line, adjust the fence position.

- [ ] **Step 5: Verify README still renders**

Run: `head -5 README.md && grep -A2 '<!-- AUTO-UPDATED:FEATURES' README.md | head -5 && tail -5 README.md`

Expected: title still at line 1, fence opens just before `## Features`, end of README unchanged.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs(readme): add auto-updater fence around Features section"
```

---

## Task 2: Create the state directory and bootstrap state file

**Files:**
- Create: `docs/audits/auto-readme/state.json`
- Create: `docs/audits/auto-readme/README.md`

- [ ] **Step 1: Create the state file**

Create `docs/audits/auto-readme/state.json` with this exact content:

```json
{
  "schema_version": 1,
  "last_processed_sha": null,
  "last_run_at": null,
  "last_run_status": null,
  "last_pr_url": null,
  "runs": []
}
```

`last_processed_sha: null` is the bootstrap sentinel — the skill's first run will discover the SHA at which `README.md` was last modified and use that as the starting point.

- [ ] **Step 2: Create the directory README**

Create `docs/audits/auto-readme/README.md` with this exact content:

```markdown
# Auto-readme audit directory

This directory is managed by the `auto-readme-tick` skill at
`.claude/skills/auto-readme-tick/SKILL.md`.

## Contents

- `state.json` — cursor and run history. Schema in the skill prose.
- `runs/YYYY-MM-DD.md` — one summary per tick.
- `dry-runs/YYYY-MM-DD-<shortsha>.md` and `.diff` — dry-run outputs.

## Manual recovery

If the skill opens a bad PR (hallucination, wrong scope), close the PR
and optionally revert `state.json` one entry back in `runs[]`. The next
tick reprocesses cleanly.

See `docs/superpowers/specs/2026-05-15-readme-auto-updater-design.md`
for the full design.
```

- [ ] **Step 3: Verify JSON parses**

Run: `python -c "import json; json.load(open('docs/audits/auto-readme/state.json'))"`

Expected: no output (success). If a `json.JSONDecodeError` fires, fix the file.

- [ ] **Step 4: Commit**

```bash
git add docs/audits/auto-readme/state.json docs/audits/auto-readme/README.md
git commit -m "docs(auto-readme): bootstrap state file and directory"
```

---

## Task 3: Write the `auto-readme-tick` skill prose

This is the core of the work. The skill is prose — no code — but every section must be executable instructions the running model can follow without ambiguity.

**Files:**
- Create: `.claude/skills/auto-readme-tick/SKILL.md`

- [ ] **Step 1: Create the skill directory**

Run: `mkdir -p .claude/skills/auto-readme-tick`

Expected: directory exists, `ls .claude/skills/auto-readme-tick/` returns empty.

- [ ] **Step 2: Write `SKILL.md`**

Create `.claude/skills/auto-readme-tick/SKILL.md` with this content:

````markdown
---
name: auto-readme-tick
description: Sync the Features section of README.md with code. Reads feat(...) commits since last sync, reads the touched source files, regenerates the fenced ## Features section, and opens a PR. Use when invoked manually as `/auto-readme-tick`, when fired by the weekly cron, or when the user explicitly says "run the readme tick" or "update the readme". Add `dry-run` to skip the PR and write a proposed diff to docs/audits/auto-readme/dry-runs/ instead.
---

# Auto-readme tick

Keep `README.md`'s Features section in sync with the actual code. One tick = one
walk through the `feat(...)` commits since the last sync, one Claude generation,
one PR.

## Hard constraints (apply to every tick)

- **Read-only outside `README.md`'s fence and `docs/audits/auto-readme/**`.**
  Refuse to write anywhere else.
- **No commits to master.** Always open a PR.
- **No `git push --force`, no `--force-with-lease`, no rebase.**
- **No new dependencies.** No package installs.
- **No container starts.** The skill needs only `git` and `gh`.
- **No external network fetches.** Only `git fetch origin`, `git push`, `gh pr create`.
- **No stacked auto-PRs.** If a prior auto-readme PR is still open, refuse.
- **Atomic state writes.** Write to `state.json.tmp`, then rename.

## Budget

- Hard cap: **4 hours wall-clock** per tick, or until usage runs out.
- Soft checkpoint every ~10 minutes: persist `state.json` and a partial run summary.
- On budget exhaustion during file-read or generation: do **not** generate / do
  **not** open PR / do **not** advance `last_processed_sha`. Status
  `budget-exceeded`. Next tick retries with fresh budget.
- After PR is opened, advance sha and persist immediately. Budget exhaustion
  after that point is harmless.
- **No partial PRs.** Either complete or none.

## Pre-flight refusals

Before doing anything else, check each of these. If any fails, write a one-line
explanation to the user and exit cleanly. Do not write to state.json or open
anything.

| Condition | What to check |
|---|---|
| `state.json` malformed | `python -c "import json; json.load(open('docs/audits/auto-readme/state.json'))"` succeeds |
| Fence markers wrong | exactly one `<!-- AUTO-UPDATED:FEATURES` and one `<!-- /AUTO-UPDATED:FEATURES -->` in README.md |
| Working tree dirty | `git status --porcelain` produces no output |
| Prior auto-readme PR open | `gh pr list --state open --search "head:readme/auto-update-"` returns nothing |
| `git fetch` works | `git fetch origin master` succeeds |
| `gh` authenticated | `gh auth status` succeeds |

## Mode dispatch

Two modes only:

- **dry-run:** invoked as `/auto-readme-tick dry-run`. Run steps 1–9 (read state,
  scan, read files, generate, validate). **Skip** branch creation, push, PR,
  state advancement. **Write** the proposed full README to
  `docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.md` and a unified diff
  to `docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.diff`. Status
  `dry-run`. Do not update `last_processed_sha`.
- **live:** invoked as `/auto-readme-tick` (no args). Full flow including PR.

## The flow

### 1. Read state

Read `docs/audits/auto-readme/state.json`. If `schema_version != 1`, refuse and
tell the user. Stash `last_processed_sha`.

### 2. Bootstrap (if needed)

If `last_processed_sha` is `null`:

```bash
git log -1 --follow --format=%H -- README.md
```

That SHA is the de-facto last README sync point. Use it as
`last_processed_sha` for this tick (do not yet persist it).

### 3. Scan feat commits

```bash
git fetch origin master
git log --grep='^feat' <last_processed_sha>..origin/master --pretty=%H
```

Reverse the list (oldest first) so the read order matches the order features
shipped.

If empty:

- Status `no-changes`.
- Set `last_processed_sha` to `git rev-parse origin/master`.
- Update `state.json`, write today's `runs/YYYY-MM-DD.md`, exit.

### 4. Gather touched files

For each commit:

```bash
git show --name-only --pretty=format: <sha>
```

Union the file lists. Drop empty lines.

### 5. Filter the file list

Drop any file matching:

- `package-lock.json`, `*.csproj.lscache`, `*.csproj.user`
- Anything under `dist/`, `node_modules/`, `bin/`, `obj/`
- `**/*.snap`, `**/*.min.*`
- Anything under `references/` (vendored)
- Anything under `PythonDataService/tests/fixtures/`

Then keep only files with extensions: `.ts`, `.html`, `.scss`, `.cs`, `.py`,
`.md`, `.json`, `.yaml`, `.yml`, `.sh`, `.ps1`, `.toml`, `.txt`.

### 6. Read current contents

For each surviving file: read its current contents at `origin/master`
(not historical, not local working tree):

```bash
git show origin/master:<file>
```

Skip any file where the show fails (file was deleted on master). That's a signal
the feature was removed; the regenerated Features section should drop the
corresponding entry.

### 7. Extract current Features section

Read `README.md`. Locate the fence. Extract the bytes between (exclusive of)
the markers. Stash as `old_fence_content`.

### 8. Generate

Single Claude call. Provide:

- `old_fence_content`
- The list of feat commits (SHA + subject)
- The current contents of all files from step 6

Prompt:

> Given the current Features section of a project README and the source of files
> recently touched by feat(...) commits, produce an updated Features section.
>
> Rules:
> - **Integrate** new capabilities visible in the touched source.
> - **Preserve** existing well-written entries verbatim where the underlying code
>   still exists and behaves as described.
> - **Remove** entries describing code that no longer exists.
> - **Do not fabricate.** If the source doesn't show a capability, don't write
>   about it.
> - Match the structure and tone of the existing section (## Features as the top
>   heading, ### subsection headers, prose paragraphs and bullet lists).
> - Output only the new section body. Do not include the fence markers
>   themselves. Do not include commentary.

### 9. Validate the generation

Reject (status `malformed-generation`, do not advance sha, exit) if any of:

- Output contains either fence marker
- Output is shorter than 200 chars
- Output doesn't start with `## Features`
- Output's first non-blank line doesn't match `^## Features\s*$`

If output passes validation, compute the byte-level diff against
`old_fence_content`. If identical: status `no-changes`, advance sha, exit.

If diff size > 80% of fence size, mark the run as `high-churn` — still
proceed, but the PR body will warn the reviewer.

### 10. Dry-run branch

If mode is `dry-run`:

- Reconstruct the full proposed README (everything outside the fence
  unchanged, fence content replaced).
- Write to `docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.md`
  (where `<shortsha>` is `git rev-parse --short origin/master`).
- Run `diff -u README.md docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.md
  > docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.diff` (the
  non-zero exit from diff is expected).
- Status `dry-run`. Do not advance `last_processed_sha`. Update
  `last_run_at` and `last_run_status` only.
- Write `runs/YYYY-MM-DD.md` (see template below).
- Tell the user where to find the dry-run artifacts. Exit.

### 11. Live branch (PR open)

If mode is `live`:

- Branch name: `readme/auto-update-YYYY-MM-DD-<shortsha>`.
- Create the branch from `origin/master`:
  ```bash
  git checkout -b readme/auto-update-YYYY-MM-DD-<shortsha> origin/master
  ```
- Replace the fence content in `README.md` with the validated output.
- Commit:
  ```bash
  git add README.md
  git commit -m "docs(readme): auto-sync features (<N> feat commits since <shortold>)"
  ```
- Push:
  ```bash
  git push -u origin readme/auto-update-YYYY-MM-DD-<shortsha>
  ```
- Open the PR with `gh pr create` using the body template below.
- Capture the PR URL.

### 12. Persist state

Update `state.json` atomically:

- `last_processed_sha` → `git rev-parse origin/master`
- `last_run_at` → ISO 8601 with offset
- `last_run_status` → `pr-opened` (or `dry-run`, `no-changes`, etc.)
- `last_pr_url` → URL from step 11 (or `null` for dry-run / no-changes)
- Append a row to `runs[]`

Write to `state.json.tmp`, then `mv state.json.tmp state.json`.

### 13. Write the run summary

Create `docs/audits/auto-readme/runs/YYYY-MM-DD.md`:

```markdown
# Auto-readme run — YYYY-MM-DD

**Mode:** dry-run | live
**Started:** <iso>
**Ended:** <iso>
**Status:** pr-opened | no-changes | dry-run | budget-exceeded | malformed-generation | error
**SHA range:** <old>..<new>
**Feat commits:** N
**Files read:** F
**High-churn:** yes | no
**PR:** <url or n/a>

## Commits processed
- <sha> — <subject>

## Notes
- <anything notable: budget hit, validation failure, etc.>
```

## PR body template

```markdown
## Auto-sync of README Features section

Triggered by N `feat(...)` commits merged to master since the last README sync.

### Commits processed

- `<sha>` — <subject>
- ...

### Stats

- Fence diff: +X / -Y lines
- Files read: F
- Run status: pr-opened {⚠ large-rewrite if high-churn}

### Reviewer checklist

- [ ] Fence diff describes real shipped features, not aspirational
- [ ] No fabricated capabilities
- [ ] No content outside the fence was touched
- [ ] Deleted entries correspond to actually-removed code

### State

- last_processed_sha: `<old>` → `<new>`
- PR opened by: auto-readme-tick @ <ISO timestamp>

🤖 Generated by the auto-readme-tick skill
```

## Recovery & rollback

If a tick opened a bad PR:

1. Close the PR (and delete the branch via `gh pr close --delete-branch`).
2. Optionally revert `state.json` to the prior `last_processed_sha` (one entry
   back in `runs[]`).
3. Next tick reprocesses cleanly.

If `state.json` is corrupted, delete it. The next tick re-bootstraps from
the SHA where `README.md` was last modified.

## When you don't know

If the state file is missing, malformed, or describes a `schema_version` this
skill doesn't recognize: stop, write nothing, tell the user.
````

- [ ] **Step 3: Verify the skill loads**

Run: `cat .claude/skills/auto-readme-tick/SKILL.md | head -5`

Expected: starts with `---\nname: auto-readme-tick\n...`. If frontmatter is malformed, fix.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/auto-readme-tick/SKILL.md
git commit -m "feat(auto-readme): add auto-readme-tick skill prose"
```

---

## Task 4: Commit the plan and open PR-A

**Files:**
- `docs/superpowers/plans/2026-05-15-readme-auto-updater.md` (this file)

- [ ] **Step 1: Commit the plan**

```bash
git add docs/superpowers/plans/2026-05-15-readme-auto-updater.md
git commit -m "docs(plan): readme auto-updater implementation plan"
```

- [ ] **Step 2: Push the branch**

```bash
git push -u origin feat/readme-auto-updater
```

Expected: push succeeds, branch tracked.

- [ ] **Step 3: Open PR-A**

```bash
gh pr create --title "feat(auto-readme): add auto-readme-tick skill + fence" --body "$(cat <<'EOF'
## Summary

Introduces a bounded, human-gated automation that keeps the Features section of `README.md` in sync with code. Triggered manually via `/auto-readme-tick` and weekly via the `schedule` skill (cron registration is a post-merge step). Output is PR-only, scoped to a fenced `<!-- AUTO-UPDATED:FEATURES -->` block.

This PR introduces the **skill, state, and fence**. The first auto-generated README modernization PR (PR-B) is produced by running the skill after this merges.

## What's in this PR

- `README.md` — wrapped existing `## Features` section in `<!-- AUTO-UPDATED:FEATURES -->` fence (no content change yet).
- `docs/audits/auto-readme/state.json` — bootstrap state with `last_processed_sha: null`.
- `docs/audits/auto-readme/README.md` — directory explainer.
- `.claude/skills/auto-readme-tick/SKILL.md` — the skill prose (~250 lines): pre-flight refusals, mode dispatch (live / dry-run), the 13-step flow, PR body template, recovery.
- `docs/superpowers/specs/2026-05-15-readme-auto-updater-design.md` — full design doc.
- `docs/superpowers/plans/2026-05-15-readme-auto-updater.md` — implementation plan.

## Validation

- `python -c "import json; json.load(open('docs/audits/auto-readme/state.json'))"` — parses
- `grep -c '<!-- AUTO-UPDATED:FEATURES' README.md` → `1`
- `grep -c '<!-- /AUTO-UPDATED:FEATURES -->' README.md` → `1`

## Post-merge steps (not in this PR)

1. Run `/auto-readme-tick dry-run` on master. Review the dry-run artifacts under `docs/audits/auto-readme/dry-runs/`.
2. If the dry-run looks good, run `/auto-readme-tick` to open PR-B (the initial README modernization).
3. Register the weekly cron via the `schedule` skill: `weekly Sunday 03:00 local → /auto-readme-tick`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## Self-review

After the four tasks above are complete, verify the spec is fully covered:

| Spec section | Implementation |
|---|---|
| §1 Architecture & file layout | Tasks 1, 2, 3 produce all the files; cron is a post-merge step (called out in PR body) |
| §2.1 Fence | Task 1 |
| §2.2 State file | Task 2 (schema in state.json + documented in SKILL.md "The flow → step 12") |
| §2.3 Skill steps 1–15 | Task 3 (SKILL.md "The flow" steps 1–13 — flow was tightened from 15 steps in spec to 13 by merging adjacent state-update steps; semantics identical) |
| §3.1 Normal tick flow | Task 3 SKILL.md "The flow" |
| §3.2 Bootstrap | Task 3 SKILL.md step 2 (uses `--follow` per spec §7 Q1 decision) |
| §3.3 File filter | Task 3 SKILL.md step 5 |
| §3.4 PR body template | Task 3 SKILL.md "PR body template" |
| §4.1 Pre-flight refusals | Task 3 SKILL.md "Pre-flight refusals" |
| §4.2 Scope guards | Task 3 SKILL.md "Hard constraints" |
| §4.3 Generation validation | Task 3 SKILL.md "The flow" step 9 |
| §4.4 Budget | Task 3 SKILL.md "Budget" |
| §4.5 Failure recovery | Task 3 SKILL.md "Recovery & rollback" |
| §4.6 What the skill never does | Task 3 SKILL.md "Hard constraints" |
| §5.1 Dry-run mode | Task 3 SKILL.md "Mode dispatch" + step 10 |
| §5.2 Skill-introduction PR validation | Task 4 PR body validation section |
| §5.3 Per-tick PR review | Task 3 SKILL.md "PR body template" → "Reviewer checklist" |
| §5.4 Rollback | Task 3 SKILL.md "Recovery & rollback" |
| §7 Open question 1 (bootstrap SHA) | Task 3 step 2 uses `git log -1 --follow --format=%H -- README.md` |
| §7 Open question 2 (cron in same PR) | Deferred to post-merge step in PR body (autonomous decision — cron without the skill landed has no meaning, and the user wants to verify dry-run before going live) |
| §7 Open question 3 (fence pre-populated) | Task 1 Step 2 — wraps existing content in place |

---

## Post-merge steps

These run after PR-A merges and are NOT part of this PR:

### Step A: Dry-run on master

```bash
git checkout master && git pull origin master
```

Then invoke `/auto-readme-tick dry-run`. The skill:

1. Reads `state.json` → `last_processed_sha: null`
2. Bootstraps via `git log -1 --follow --format=%H -- README.md`
3. Scans all `feat(...)` commits since that SHA
4. Reads the touched source files
5. Generates a proposed Features section
6. Writes dry-run artifacts to `docs/audits/auto-readme/dry-runs/`

Review `docs/audits/auto-readme/dry-runs/<date>-<sha>.diff` carefully. If it looks good, proceed. If not, iterate on the SKILL.md prompt and re-run.

### Step B: Live run (opens PR-B)

Once dry-run looks good, run `/auto-readme-tick` (no args). This opens PR-B with the actual updated Features section.

Review PR-B using the checklist in its body. Merge when satisfied.

### Step C: Register the weekly cron

Invoke the `schedule` skill to register a weekly cron:

- Schedule: Sunday 03:00 America/Chicago
- Action: invoke `/auto-readme-tick`
- Idempotent — safe to re-run if cron registration drifts

After this, the README stays sync'd automatically. Manual `/auto-readme-tick` remains available.
