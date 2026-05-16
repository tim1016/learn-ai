---
name: auto-readme-tick
description: Sync the Features section of README.md with code. Reads feat(...) commits since the last sync, reads the touched source files, regenerates the fenced ## Features section, and opens a PR. Use when invoked manually as `/auto-readme-tick`, when fired by the weekly cron, or when the user explicitly says "run the readme tick" or "update the readme". Add `dry-run` to skip the PR and write a proposed diff to docs/audits/auto-readme/dry-runs/ instead.
---

# Auto-readme tick

Keep `README.md`'s Features section in sync with the actual code. One tick =
one walk through the `feat(...)` commits since the last sync, one Claude
generation, one PR (or one dry-run artifact).

Design doc: `docs/superpowers/specs/2026-05-15-readme-auto-updater-design.md`.

## Hard constraints (every tick)

- **Read-only outside `README.md`'s fence and `docs/audits/auto-readme/**`.**
  Refuse to write anywhere else.
- **No commits to master.** Always open a PR.
- **No `git push --force`, no `--force-with-lease`, no rebase.**
- **No new dependencies.** No package installs.
- **No container starts.** The skill needs only `git` and `gh`.
- **No external network fetches** beyond `git fetch origin`, `git push`,
  `gh pr create`.
- **No stacked auto-PRs.** If a prior auto-readme PR is still open, refuse.
- **Atomic state writes.** Write to `state.json.tmp`, then rename.

## Budget

- Hard cap: **4 hours wall-clock** per tick, or until usage runs out.
- Soft checkpoint every ~10 minutes: persist `state.json` and a partial run
  summary, so a forced exit leaves clean state.
- **Budget exhausted during file-read or generation:** do **not** generate /
  do **not** open PR / do **not** advance `last_processed_sha`. Status
  `budget-exceeded`. Next tick retries with fresh budget.
- **After PR is opened:** state already advanced and persisted. Budget
  exhaustion after this point is harmless.
- **No partial PRs.** Either the PR is complete and accurate, or there is
  no PR.

## Pre-flight refusals

Before doing anything else, check each of these. If any fails, write a
one-line explanation to the user and exit cleanly. Do not write to
`state.json` and do not open anything.

| Condition | How to check |
|---|---|
| `state.json` malformed | `python -c "import json; json.load(open('docs/audits/auto-readme/state.json'))"` succeeds |
| Fence markers wrong | exactly one `<!-- AUTO-UPDATED:FEATURES` and one `<!-- /AUTO-UPDATED:FEATURES -->` in `README.md` |
| Working tree dirty | `git status --porcelain` produces no output |
| Prior auto-readme PR open | `gh pr list --state open --search "head:readme/auto-update-"` returns nothing |
| `git fetch` works | `git fetch origin master` succeeds |
| `gh` authenticated | `gh auth status` succeeds |

## Mode dispatch

Two modes only:

- **dry-run:** invoked as `/auto-readme-tick dry-run`. Run steps 1–9 (read
  state, scan, read files, generate, validate). **Skip** branch creation,
  push, PR, state advancement. **Write** the proposed full README to
  `docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.md` and a unified
  diff to `.../dry-runs/YYYY-MM-DD-<shortsha>.diff`. Status `dry-run`. Do
  not update `last_processed_sha`.
- **live:** invoked as `/auto-readme-tick` (no args). Full flow including
  branch, push, and PR.

## The flow

### 1. Read state

Read `docs/audits/auto-readme/state.json`. If `schema_version != 1`, refuse
and tell the user. Stash `last_processed_sha`.

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
- Anything under `references/` (vendored reference code)
- Anything under `PythonDataService/tests/fixtures/` (golden fixtures, not
  feature-bearing)

Then keep only files with these extensions: `.ts`, `.html`, `.scss`, `.cs`,
`.py`, `.md`, `.json`, `.yaml`, `.yml`, `.sh`, `.ps1`, `.toml`, `.txt`.

### 6. Read current contents

For each surviving file: read its current contents at `origin/master`
(not historical, not local working tree):

```bash
git show origin/master:<file>
```

Skip any file where `git show` fails (file was deleted on master). That's a
signal the feature was removed; the regenerated Features section should
drop the corresponding entry.

### 7. Extract current Features section

Read `README.md`. Locate the fence. Extract the bytes between (exclusive
of) the markers. Stash as `old_fence_content`.

### 8. Generate

Single Claude call. Provide:

- `old_fence_content`
- The list of feat commits (SHA + subject)
- The current contents of all files from step 6

Prompt:

> Given the current Features section of a project README and the source of
> files recently touched by feat(...) commits, produce an updated Features
> section.
>
> Rules:
> - **Integrate** new capabilities visible in the touched source.
> - **Preserve** existing well-written entries verbatim where the underlying
>   code still exists and behaves as described.
> - **Remove** entries describing code that no longer exists.
> - **Do not fabricate.** If the source doesn't show a capability, don't
>   write about it.
> - Match the structure and tone of the existing section (## Features as
>   the top heading, ### subsection headers, prose paragraphs and bullet
>   lists).
> - Output only the new section body, starting with `## Features`. Do not
>   include the fence markers themselves. Do not include commentary.

### 9. Validate the generation

Reject (status `malformed-generation`, do not advance sha, exit) if any of:

- Output contains either fence marker
- Output is shorter than 200 chars
- Output's first non-blank line doesn't match `^## Features\s*$`

If output passes validation, compute the byte-level diff against
`old_fence_content`. If identical: status `no-changes`, advance sha, exit.

If diff size > 80% of the fence size, mark the run as `high-churn` — still
proceed, but the PR body will warn the reviewer.

### 10. Dry-run output

If mode is `dry-run`:

- Reconstruct the full proposed README (everything outside the fence
  unchanged, fence content replaced).
- Write to `docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.md`
  where `<shortsha>` is `git rev-parse --short origin/master`.
- Run `diff -u README.md docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.md
  > docs/audits/auto-readme/dry-runs/YYYY-MM-DD-<shortsha>.diff`
  (the non-zero exit from `diff` is expected).
- Status `dry-run`. Do not advance `last_processed_sha`. Update
  `last_run_at` and `last_run_status` only.
- Write `runs/YYYY-MM-DD.md` (template below).
- Tell the user where to find the dry-run artifacts. Exit.

### 11. Live output (PR open)

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
- Open the PR with `gh pr create` using the PR body template below.
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

1. Close the PR and delete the branch:
   ```bash
   gh pr close <pr-number> --delete-branch
   ```
2. Optionally revert `state.json` to the prior `last_processed_sha` (one
   entry back in `runs[]`).
3. Next tick reprocesses cleanly.

If `state.json` is corrupted: delete it. The next tick re-bootstraps from
the SHA where `README.md` was last modified.

## When you don't know

If the state file is missing, malformed, or describes a `schema_version`
this skill doesn't recognize: stop, write nothing, tell the user.
