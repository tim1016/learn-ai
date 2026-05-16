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
