# Current Documentation Entry Point

Use this file when you need the current docs map without loading every plan,
audit, and handoff in the repository.

## Start Here

1. `AGENTS.md` — cross-agent operating rules and repo philosophy.
2. `CLAUDE.md` — Claude Code-specific project instructions.
3. `docs/doc-authority.md` — authoritative map of canonical, supporting, and archived docs.

## Core Authority Docs

- `docs/math-sources-of-truth.md` — canonical implementation per math concept.
- `docs/architecture/engine-authority-map.md` — which engine owns each job.
- `docs/architecture/numerical-authority-migration-plan.md` — active math-authority migration sequence.
- `.claude/rules/numerical-rigor.md` — tolerances, golden fixtures, timestamp rules, reconciliation taxonomy.

## Current Cleanup Notes

- Agent-facing rules and skills live under `.claude/`, not `.Codex/`.
- Generated and runtime-heavy paths are hidden from normal `rg` searches by `.rgignore`.
- Historical plans and audit findings are useful provenance, but implementation authority should flow through `docs/doc-authority.md`.
