---
name: meta-propose-skill
description: Propose a new skill when a repeating task pattern emerges. Use when user says "we keep doing this", "this is the third time", "can you make a skill for", or when observing three similar task shapes in the same session or across sessions.
---

# Meta: Propose a New Skill

When a recurring task shape emerges, stop and propose a new skill instead of just doing the task again. Prevents skill sprawl (driven by the user asking "make a skill for X" reactively) while capturing genuine patterns.

## When to propose

- The same task shape has appeared **three or more times** across sessions
- The task has non-obvious steps that must be done in a specific order
- The task has domain knowledge or conventions that aren't in existing skills
- Skipping a step in the task typically causes bugs or rework

## When NOT to propose

- One-off task, even if elaborate — it doesn't repeat, so it doesn't earn a skill
- Task already covered by an existing skill (extend the skill instead)
- Task is a simple sequence of steps with no domain knowledge — a shell script or a README entry is a better fit

## Proposal format

When proposing a new skill, produce this:

```markdown
## Proposed Skill: <gerund-form-name>

**Trigger phrases** (what should activate it):
- "..."
- "..."
- "..."

**When NOT to use:**
- ...

**Recurring pattern observed:**
<Describe the task shape. What does the user ask? What does Claude do? Which steps are non-obvious?>

**Proposed execution phases:**
1. ...
2. ...
3. ...

**What makes this skill-worthy** (vs a one-off task or a doc):
- ...

**Overlap with existing skills:**
- `<skill-name>`: <how this new skill is distinct or how they compose>

**Files the skill would reference:**
- ...

**Alternatives considered:**
- Extending `<existing-skill>`: <why rejected>
- Just documenting the pattern: <why rejected>
```

## Before proposing, check

1. **Search existing skills.** Read the description of every skill in `.claude/skills/*/SKILL.md` and confirm none already covers this. If one partially covers it, propose an extension rather than a new skill.
2. **Check `.claude/rules/`.** If the pattern is really a *convention* (not a task), it might belong in a rules file, not a skill.
3. **Confirm with the user** before generating the `SKILL.md`. Skill creation is not a reflex — each skill adds description text to every Claude Code session's context. Noise has a cost.

## Skill authoring rules

When the user approves a proposal and asks you to write the `SKILL.md`:

- **Name in gerund form** where it fits ("port-indicator" becomes "porting indicators" in description). Kebab-case in the directory name.
- **Description must include trigger phrases** the user would actually say. Anthropic's own skill-authoring docs show activation can jump from 20% to 90% with better descriptions — this is the single most important part.
- **Keep under ~500 lines.** Reference additional files if more detail is needed.
- **Imperative/infinitive voice**: "Port the indicator" not "You should port the indicator".
- **Explicit "When NOT to use" section**, not just "When to use".
- **Execution phases numbered and in strict order** where order matters.
- **Anti-patterns section at the end** listing specific things to avoid.

## Iteration

After a new skill is in place for two weeks:

- Observe whether Claude actually triggers it, or has to be invoked manually
- If it's not triggering, the description needs trigger phrases that match how the user actually talks about the task
- If it's triggering too eagerly on unrelated tasks, the description is too broad

Report these observations and propose description edits.

## Anti-patterns to avoid

- Proposing a skill for a single occurrence (three or nothing)
- Proposing a skill when a rules file or README is the better fit
- Writing the `SKILL.md` before the user approves the proposal
- Skipping the "overlap with existing skills" analysis
- Description too generic ("helps with Python work") — must be specific about triggers and scope
