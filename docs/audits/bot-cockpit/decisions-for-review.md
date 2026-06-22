# Decisions for review — Bot Cockpit audit 2026-06-22

Per §3.5 of the run prompt, every uncertain judgment call lands here for the user to read on completion. The auto-merge gate fires regardless of whether this file is empty — these are *parallel* artifacts, not blockers.

---

## D-001 — Wrapper failure pre-empted the unattended overnight run

**Context.** The scheduled run at 02:05 CT 2026-06-22 fired the launchd plist correctly. The wrapper script (`docs/audits/bot-cockpit/run-2026-06-22.sh`) called `/usr/bin/timeout` to enforce the outer 6h15m safety cap. macOS does not ship a `/usr/bin/timeout` binary — `timeout(1)` is part of GNU coreutils, available only as `gtimeout` after `brew install coreutils`. The wrapper exited 127 (command not found) before invoking Claude, and the plist self-unloaded.

**Decision.** When the user asked me to "fire the prompt that the bot was going to do tonight" the morning after the failed run, I executed the prompt interactively in the live session with the user awake. This trades the unattended-overnight execution model for an interactive one. I did not amend the wrapper here; that is a separate follow-up (`gtimeout` if coreutils is available, or just drop the outer `timeout` layer since the prompt enforces 6h internally).

**Alternatives considered.** (a) Just fix the wrapper and re-schedule for the next overnight window. (b) Manually execute the wrapper now from a foreground shell. (c) Decompose the prompt and run it interactively. The user chose (c) explicitly via AskUserQuestion.

**Citations.** `docs/audits/bot-cockpit/run-2026-06-22.log` (the failed run record).

---

## D-002 — Thermo invocation policy for this run

**Context.** The auto-memory file `feedback_pr_workflow.md` records that the user invokes `thermo-nuclear-code-quality-review` themselves (the skill has `disable-model-invocation: true`). The run prompt's §3.4 gate 4 says I should invoke thermo myself before first push. These conflict.

**Decision.** Per the AskUserQuestion answer at the top of this session, the user explicitly chose "I run thermo per the prompt (prompt wins)" — memory rule overridden for this single run.

**Citations.** AskUserQuestion answer at session start; `feedback_pr_workflow.md`; run-prompt §3.4 gate 4.

---

## D-003 — Auto-merge policy

**Context.** §3.4 of the prompt says auto-merge proceeds if and only if every gate is green. The user is awake during this run.

**Decision.** I will still apply the §3.4 gate test. If any gate is anything other than fully green — including project-scope lint / test regressions over the baseline, any open P0/P1 findings, missing browser evidence for affordances I changed, or unaddressed thermo major findings — I leave the PR as draft, write `STATUS: DRAFT PR OPEN — REVIEW NEEDED`, and exit. I do not auto-merge on partial green or on "looks good enough." This matches the prompt's intent that interrupted/incomplete runs never auto-merge.

**Citations.** Run-prompt §3.4 gates; §3.2 graceful degradation final clause ("Interrupted runs never auto-merge").

---

## D-004 — Bot-cockpit untracked artifacts from the failed run are included in the branch

**Context.** The failed overnight run left `docs/audits/bot-cockpit/{run-prompt-2026-06-22.md, run-2026-06-22.sh, run-2026-06-22.log, launchd-stdout.log, launchd-stderr.log}` as untracked files on master at `77bf6563`.

**Decision.** Commit these onto the audit branch as part of the workspace scaffold so the PR carries the full picture: the original prompt, the wrapper that failed, the launchd evidence, and the artifacts produced this morning. They are the historical record of why the interactive run happened.

**Citations.** `docs/audits/bot-cockpit/run-2026-06-22.log` shows exit code 127.
